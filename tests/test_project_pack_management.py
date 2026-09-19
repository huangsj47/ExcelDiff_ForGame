# -*- coding: utf-8 -*-
"""项目专属知识包（`skills/projects/<项目代号>/`）的界面化维护。

## 这个文件守的四件事

1. **路径安全**。这一组接口第一次让「用户在界面上输入的字符串」变成磁盘路径。
   挡不住的后果不是样式错乱，而是**任意文件写**：`../` 能写到别的项目的知识包里，
   绝对路径能写到仓库外面，而 `skills/version-diff-review/` 是平台内置的评审规程 ——
   从项目页改写它，等于一个项目管理员能改掉所有项目的评审上下文。
2. **契约闸门真的在写之前**。界面上出现「存进去了但校验不通过」的半截状态，
   用户下一次跑分析才会发现，而那时已经不知道该回滚哪一份文件。
3. **权限**。与 `/config` 同口径：读要项目成员，写要项目管理员。
4. **溯源接线是真的**。改完知识包，`skill_version` 必须变 —— 否则历史结论会被
   当成新规则下的结论复用，而界面上看起来一切正常。

## 一个必须写下来的设计推论

`skill_contract._check_references()` 是**双向**的（正文提到的文件必须存在；
存在的文件必须被正文提到）。由此可得：**「先建文档再改清单」和「先改清单再建文档」
两种顺序都不成立** —— 无论哪种，中间总有一次保存会撞上双向校验。

所以本文件里有一整组用例在守同一件事：**引用行与文件必须在同一次写入里落地**
（`write_reference` 补引用行、`delete_reference` 摘引用行、`create_pack_from_template`
一次把清单与起始文档一起写）。没有这一条，界面上会出现一个「怎么点都失败」的按钮，
而这在用户看来就是平台坏了。
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from flask import make_response
from werkzeug.exceptions import HTTPException

import routes.ai_analysis_routes as ai_routes
from app import app as flask_app
from app import create_tables, db
from models import Project
from services.ai import project_pack_service
from services.ai.skill_contract import PROJECT_PACK_MANIFEST, validate_project_pack
from services.ai.skill_loader import SKILL_PROJECTS_ROOT_ENV, load_skills

REPO_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_WITH_MENTION = "---\nname: pack\ndescription: 项目知识包\n---\n\n# 包\n\n可读文档：`spec.md`\n"
REFERENCE_BODY = "# 配表规范\n\nID 为 6 位。\n"


# ==========================================================================
# 夹具
# ==========================================================================


@pytest.fixture()
def projects_root(tmp_path, monkeypatch):
    """把知识包根目录指到临时目录。

    这一条同时是全文件的**安全前提**：测试期间任何一次写入都不可能碰到仓库里真实的
    `skills/projects/g119/`，也不可能碰到 `skills/version-diff-review/`。
    """
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(root))
    return root


@pytest.fixture()
def project_id():
    with flask_app.app_context():
        create_tables()
        project = Project(code=f"p{uuid.uuid4().hex[:8]}", name="知识包测试项目")
        db.session.add(project)
        db.session.commit()
        return project.id


def _code_of(project_id: int) -> str:
    with flask_app.app_context():
        return db.session.get(Project, project_id).code


def _set_code(project_id: int, code: str) -> None:
    with flask_app.app_context():
        project = db.session.get(Project, project_id)
        project.code = code
        db.session.commit()


def _allow(monkeypatch, *, access=True, admin=True):
    """权限判定的替身。

    本文件里几乎所有用例都 `_allow(monkeypatch)` —— 那是**故意的**：权限本身有专章
    （第七节）逐端点守着，其余的用例要测的是安全与契约，不该被权限判定挡住。
    """
    monkeypatch.setattr(ai_routes, "_has_project_access", lambda _pid: access)
    monkeypatch.setattr(ai_routes, "_has_project_admin_access", lambda _pid: admin)


class _ViewClient:
    """像 `tests/test_ai_config_routes.py` 那样直接调 view。

    认证链挂在 `before_request` 上，`test_client` 走不到 view 就先被 401 拦住了；
    直接调 view 正好**只测 view 内部**的权限判定 —— 那才是本文件要守的东西。
    """

    def get(self, url, **kwargs):
        return self._call("GET", url)

    def put(self, url, json=None, **kwargs):
        return self._call("PUT", url, json)

    def post(self, url, json=None, **kwargs):
        return self._call("POST", url, json)

    def delete(self, url, **kwargs):
        return self._call("DELETE", url)

    def _call(self, method, url, body=None):
        endpoint, args = flask_app.url_map.bind("localhost").match(url, method=method)
        view = flask_app.view_functions[endpoint]
        with flask_app.test_request_context(url, method=method, json=body):
            return make_response(view(**args))


@pytest.fixture()
def client():
    return _ViewClient()


def _pack_of(projects_root: Path, project_code: str) -> Path:
    return projects_root / project_pack_service.project_pack_slug(project_code)


def _seed_pack(
    projects_root: Path, project_code: str, *, reference: str | None = "spec.md"
) -> Path:
    """在临时根下造一个最小合法知识包。"""
    pack = _pack_of(projects_root, project_code)
    (pack / "references").mkdir(parents=True, exist_ok=True)
    (pack / PROJECT_PACK_MANIFEST).write_text(
        MANIFEST_WITH_MENTION
        if reference
        else "---\nname: pack\ndescription: 空包\n---\n\n# 包\n\n（还没有文档）\n",
        encoding="utf-8",
    )
    if reference:
        (pack / "references" / reference).write_text(REFERENCE_BODY, encoding="utf-8")
    assert validate_project_pack(pack) == [], "夹具本身就不合法，后面的用例没有意义"
    return pack


def _snapshot(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def _assert_rejected(call, label: str) -> None:
    """把「被拒」的两种形态都认下来，但**只认 4xx**。

    * 走到 view 的：回 400 + 字段级明细；
    * 没走到 view 的：Werkzeug 的路由匹配就失败了（`a/b`、`..%2f` 这类含分隔符的
      名字会多切出一段路径），或规则本身不匹配空段。

    两种都是「拒绝」，但**必须是 4xx** —— 若哪天有人把校验挪到写之后再抛，
    这里会看到 500，那时就该红。
    """
    try:
        resp = call()
    except HTTPException as exc:
        assert 400 <= exc.code < 500, f"{label} 在路由层报的是 {exc.code}"
        return
    assert resp.status_code in (400, 403, 404), f"{label} 被放行了：{resp.status_code}"


# ==========================================================================
# 一、路径穿越 / 非法文件名
# ==========================================================================

# 每一个都是「如果它被放行，会落到哪里」的具体形态。
# 这份清单同时用于 HTTP 层与服务层：能在 URL 里表达的走接口，表达不了的走服务层。
HOSTILE_NAMES = [
    "../escape",
    "../../escape",
    "a/b",
    "a\\b",
    "..%2fescape",
    "..%5cescape",
    "C:/windows/system32/x",
    "C:\\windows\\x",
    "/etc/passwd",
    "\\\\server\\share\\x",
    "",
    "   ",
    ".",
    "..",
    "...",
    "con",
    "aux",
    "nul",
    "com1",
    "x" * 200,
    "UPPER",
    "with space",
    "中文名",
    "under_score",
    "-leading",
    "trailing-",
    "double--dash",
    "notes.txt",
    "notes.markdown",
    "notes.MD",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_reference_name_is_rejected_and_writes_nothing(
    client, project_id, projects_root, monkeypatch, name
):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    before = _snapshot(projects_root)

    _assert_rejected(
        lambda: client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/references/{name}",
            json={"content": "# x\n", "description": "说明"},
        ),
        repr(name),
    )

    assert _snapshot(projects_root) == before, f"{name!r} 在磁盘上留下了痕迹"


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_skill_name_is_rejected_and_writes_nothing(
    client, project_id, projects_root, monkeypatch, name
):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    before = _snapshot(projects_root)

    _assert_rejected(
        lambda: client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/skills/{name}",
            json={"description": "说明", "body": "# 正文\n"},
        ),
        repr(name),
    )

    assert _snapshot(projects_root) == before, f"{name!r} 在磁盘上留下了痕迹"


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_name_is_rejected_by_the_service_itself(name):
    """绕过路由直接调服务层：白名单必须自己成立。

    **这是「不能靠前端校验」那句话的证明**。前端的输入限制只作用于
    「用户在界面上打字」这一条路径，而接口是可以直接调的（curl / 脚本 /
    某个改坏了的页面）。安全规则必须长在服务端，前端那层只是提前告诉用户。
    """
    with pytest.raises(project_pack_service.ConfigValidationError):
        project_pack_service.normalize_skill_name(name)
    with pytest.raises(project_pack_service.ConfigValidationError):
        project_pack_service.normalize_reference_name(name)


@pytest.mark.parametrize("name", ["tab\tname", "newline\nname", "nul\x00name", "\u202egnp.txt"])
def test_control_characters_and_bidi_overrides_are_rejected(name):
    """控制字符与双向控制符必须拒绝，且这类名字在接口层也进不来。

    `\\u202e` 会让界面上显示的文件名与实际落盘的**顺序相反** —— 用户以为自己在
    删除 `gnp.txt`，实际删的是 `png.txt`。这类名字必须连创建的机会都没有。
    """
    for raw in (name, name + ".md"):
        with pytest.raises(project_pack_service.ConfigValidationError):
            project_pack_service.normalize_reference_name(raw)
        with pytest.raises(project_pack_service.ConfigValidationError):
            project_pack_service.normalize_skill_name(raw)


def test_only_markdown_documents_are_accepted():
    """只允许 `.md`；其它扩展名、以及大小写不对的 `.MD` 都拒绝。

    `.MD` 值得单独说：它在大小写不敏感的文件系统上会落成 `foo.MD`，而
    `_collect_references()` 只 `glob("*.md")` —— 于是文件在磁盘上存在、在列表里
    看不见（Windows 上恰好能看见，Linux 上会漏）。「看运气」的行为不该出现。
    """
    for raw in ("notes.txt", "notes.markdown", "notes.json", "notes.MD", "notes.Md", "a.b.md"):
        with pytest.raises(project_pack_service.ConfigValidationError):
            project_pack_service.normalize_reference_name(raw)
    # 两种合法写法（带扩展名 / 不带）指向同一个文件
    assert project_pack_service.normalize_reference_name("config-table-spec") == "config-table-spec"
    assert project_pack_service.normalize_reference_name("config-table-spec.md") == "config-table-spec"


def test_a_name_that_is_too_long_is_rejected_with_the_limit_in_the_message():
    with pytest.raises(project_pack_service.ConfigValidationError) as excinfo:
        project_pack_service.normalize_skill_name("x" * 65)
    assert "上限" in str(excinfo.value)


def test_the_error_carries_a_field_so_the_ui_can_mark_the_right_input(
    client, project_id, projects_root, monkeypatch
):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/UPPER",
        json={"content": "# x\n"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"][0]["field"] == "name"
    assert body["errors"][0]["label"] and body["errors"][0]["message"]


# ==========================================================================
# 二、绝不写平台内置 skill 目录
# ==========================================================================


def test_the_platform_skill_directory_can_never_be_written(client, project_id, monkeypatch):
    """**专项测试**：写平台内置 skill 目录必须被拒。

    两条攻击路径都试：
      1. 项目代号里塞 `../` / 绝对路径，指望 slug 之后还能往上跳；
      2. 把 `SKILL_PROJECTS_ROOT` 配成 `skills`（部署时是真会这么配错的 ——
         配成 `skills` 就等于「项目包放在 skills 下」这个意图），再让项目代号
         恰好等于 `version-diff-review`。
    """
    platform_skill = REPO_ROOT / "skills" / "version-diff-review" / "SKILL.md"
    before = platform_skill.read_bytes()

    _allow(monkeypatch)
    for hostile_code in (
        "../version-diff-review",
        "..\\..\\version-diff-review",
        "../../../../tmp/evil",
        "/etc/evil",
        "C:/evil",
    ):
        _set_code(project_id, hostile_code)
        for path_kind in ("references/spec", "skills/evil"):
            _assert_rejected(
                lambda kind=path_kind: client.put(
                    f"/ai-analysis/projects/{project_id}/knowledge/{kind}",
                    json={"content": "# x\n", "description": "说明", "body": "# x\n"},
                ),
                hostile_code,
            )
        _assert_rejected(
            lambda: client.put(
                f"/ai-analysis/projects/{project_id}/knowledge/manifest",
                json={"content": "# x\n"},
            ),
            hostile_code,
        )

    # 第二条攻击路径：把根目录配置成 `skills/` 本身。
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(REPO_ROOT / "skills"))
    _set_code(project_id, "version-diff-review")
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest",
        json={"content": "---\nname: x\ndescription: y\n---\n\n# x\n"},
    )
    assert resp.status_code == 400, "SKILL_PROJECTS_ROOT 配成 skills/ 之后能写到内置 skill 上"
    assert platform_skill.read_bytes() == before, "平台内置 skill 被改动了"


def test_the_platform_skill_directory_is_refused_by_the_service_directly(projects_root, monkeypatch):
    """服务层的同一条防线：`locate_pack` 自己就该拒绝。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(REPO_ROOT / "skills"))
    with pytest.raises(project_pack_service.ConfigValidationError):
        project_pack_service.locate_pack("version-diff-review")


def test_a_normal_project_code_whose_slug_happens_to_match_is_still_allowed(
    client, project_id, projects_root, monkeypatch
):
    """反向保险：名字里带 `version-diff-review` 的**项目包**是合法的。

    用一个宽到能把这句注释本身也判成命中的检查（比如 `in parts`），
    会连正常项目一起挡掉 —— 那也是一种缺陷。
    """
    _allow(monkeypatch)
    _set_code(project_id, "version-diff-review-notes")
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest",
        json={"content": MANIFEST_WITH_MENTION.replace("`spec.md`", "（示意）")},
    )
    assert resp.status_code == 200, resp.get_json()


def test_writes_always_land_under_the_projects_root(projects_root, monkeypatch, client, project_id):
    """反向保险：正常写入必须落在 `projects_root/<slug>/` 里面。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest",
        json={"content": "---\nname: pack\ndescription: 说明\n---\n\n# 包\n"},
    )
    assert resp.status_code == 200, resp.get_json()

    written = _pack_of(projects_root, code) / PROJECT_PACK_MANIFEST
    assert written.is_file()
    assert written.resolve().is_relative_to(projects_root.resolve())


# ==========================================================================
# 三、大小限额
# ==========================================================================


def test_an_oversized_file_is_rejected_on_the_content_field(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/huge",
        json={"content": "啊" * (project_pack_service.MAX_FILE_BYTES // 3 + 100)},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"][0]["field"] == "content"
    assert "上限" in body["errors"][0]["message"]
    pack = _pack_of(projects_root, _code_of(project_id))
    assert not (pack / "references" / "huge.md").exists()


# ==========================================================================
# 四、契约校验：错误落到具体字段，且磁盘上不留半截
# ==========================================================================


def test_a_manifest_without_frontmatter_is_rejected_with_a_field_level_error(
    client, project_id, projects_root, monkeypatch
):
    _allow(monkeypatch)
    pack = _seed_pack(projects_root, _code_of(project_id))
    before = (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest",
        json={"content": "# 忘了写 frontmatter\n"},
    )

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["errors"], "校验失败却没有明细"
    assert body["errors"][0]["field"] == "content", body["errors"]
    assert "---" in body["errors"][0]["message"]
    assert (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8") == before, "失败的一次改动了磁盘"


def test_a_manifest_missing_the_required_keys_is_rejected(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest",
        json={"content": "---\nname: pack\n---\n\n# 包\n"},
    )
    assert resp.status_code == 400
    messages = " ".join(item["message"] for item in resp.get_json()["errors"])
    assert "description" in messages


def test_an_over_long_description_is_rejected_on_the_description_field(
    client, project_id, projects_root, monkeypatch
):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/skills/toolong",
        json={"description": "字" * 2000, "body": "# 正文\n"},
    )
    assert resp.status_code == 400
    fields = {item["field"] for item in resp.get_json()["errors"]}
    assert "description" in fields, resp.get_json()["errors"]


def test_a_description_with_a_yaml_breaking_colon_is_rejected_on_that_field(
    client, project_id, projects_root, monkeypatch
):
    """`description` 里出现 `": "` 会被 YAML 当成新的键值对 —— 契约直接判非法。

    这条正是「不能让用户手写 frontmatter」的理由本身：看起来只是一句正常的说明，
    写进去却会让整份 SKILL.md 加载失败，而错误信息对策划完全不可读。
    归因必须落到 `description` 那一栏，否则用户不知道该改哪里。
    """
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/skills/badcolon",
        json={"description": "用途: 检查段位", "body": "# 正文\n"},
    )
    assert resp.status_code == 400
    assert {item["field"] for item in resp.get_json()["errors"]} == {"description"}


def test_a_manifest_mentioning_a_missing_document_is_rejected(
    client, project_id, projects_root, monkeypatch
):
    """正文里提到了但 `references/` 下没有 → 必须拒绝（模型会去索要读不到的文档）。

    这是双向校验的一个方向。另一个方向（文件存在但没被提到）由下面的
    「新建文档会补引用行」那组用例从行为上覆盖 —— 那个方向在界面上**不可能**
    被用户制造出来，因为新建文档时引用行是同一次写入里补上的。
    """
    _allow(monkeypatch)
    pack = _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest",
        json={
            "content": "---\nname: pack\ndescription: 包\n---\n\n# 包\n\n- `spec.md` —— 有\n- `ghost.md` —— 没有\n"
        },
    )
    assert resp.status_code == 400
    body = resp.get_json()
    messages = " ".join(item["message"] for item in body["errors"])
    assert "ghost.md" in messages
    assert "正文里提到了" in body["message"]
    assert "ghost.md" not in (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")


def test_a_failed_write_leaves_no_half_written_file_or_temp_files(
    client, project_id, projects_root, monkeypatch
):
    """**半截状态**：校验失败时目标文件不存在，目录里也不该留下临时文件。

    `_atomic_write` 先写 `.名字.xxxx.tmp` 再 `os.replace`，而校验发生在写之前 ——
    失败路径上连临时文件都不该出现过。这一条守的是「用户下次打开列表，
    看到一份自己没建过的文件」。
    """
    _allow(monkeypatch)
    pack = _seed_pack(projects_root, _code_of(project_id))

    for _ in range(3):
        # 空正文 → 被契约拒绝（SKILL.md 没有正文/没有 name）
        resp = client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/skills/ghost",
            json={"description": "", "body": ""},
        )
        assert resp.status_code == 400

    assert not (pack / "ghost").exists()
    leftovers = [p.name for p in pack.rglob("*") if p.name.endswith(".tmp")]
    assert not leftovers, f"留下了临时文件：{leftovers}"


def test_the_shadow_directory_is_cleaned_up_after_a_failed_validation(
    client, project_id, projects_root, monkeypatch
):
    """影子目录不能留在系统临时目录里 —— 每次失败都留一份，久了就是磁盘泄漏。

    断言的是**真实的临时目录**（`tempfile.gettempdir()`），不是 pytest 自己的
    `tmp_path`：`_apply_with_validation` 用的是 `tempfile.mkdtemp`，它落在前者里。
    对 `tmp_path` 做快照比对会是一条永远为真的假断言。
    """
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    temp_root = Path(tempfile.gettempdir())

    for _ in range(2):
        client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/skills/ghost",
            json={"description": "", "body": ""},
        )

    leftovers = [path.name for path in temp_root.glob("pack-stage-*")]
    assert leftovers == [], f"影子目录没有清理：{leftovers}"


def test_a_successful_reference_write_is_loadable_by_the_loader(
    client, project_id, projects_root, monkeypatch
):
    """写完之后 `load_skills()` 必须真的读得到它 —— 校验通过不等于接上了线。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    _seed_pack(projects_root, code)

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/spec",
        json={"content": REFERENCE_BODY, "description": "配表规范"},
    )
    assert resp.status_code == 200, resp.get_json()

    with flask_app.app_context():
        loaded = load_skills(REPO_ROOT, project_code=code)
    names = {document.name for document in loaded.project_documents}
    assert "spec.md" in names
    assert PROJECT_PACK_MANIFEST in names


# ==========================================================================
# 五、空态 / 读
# ==========================================================================


def test_a_missing_pack_is_an_empty_state_not_an_error(client, project_id, projects_root, monkeypatch):
    """目录不存在是正常状态（大多数项目都还没维护过知识），不是 500。"""
    _allow(monkeypatch)
    resp = client.get(f"/ai-analysis/projects/{project_id}/knowledge")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["exists"] is False
    assert body["entries"] == []
    assert body["slug"], "没有 slug 的话界面不知道目录名"
    assert body["scaffold"]["available"] is True, "空态必须给创建入口"
    assert body["manifest_template"].startswith("---\n"), "模板必须自带 frontmatter"


def test_the_manifest_template_is_savable_as_is(client, project_id, projects_root, monkeypatch):
    """**空白模板必须能单独存下去**。

    它在正文里一个 `.md` 都不提，正是为了这一点：自己手写清单的用户从它开始，
    如果模板一存就被「提到了但文件不存在」拒绝，用户第一步就卡死。
    """
    _allow(monkeypatch)
    template = client.get(f"/ai-analysis/projects/{project_id}/knowledge").get_json()[
        "manifest_template"
    ]

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/manifest", json={"content": template}
    )

    assert resp.status_code == 200, resp.get_json()
    pack = _pack_of(projects_root, _code_of(project_id))
    assert (pack / PROJECT_PACK_MANIFEST).is_file()


def test_the_empty_state_can_be_filled_from_the_template(client, project_id, projects_root, monkeypatch):
    """空态 → 「从模板创建」→ 建出来的包**立刻就是合法的**。

    这一条守的是双向校验带来的那个坑：建包模板的正文引用了两份起始文档，
    如果分两次写（先清单、再文档），第二次必然被「提到了但文件不存在」拒绝 ——
    于是这个按钮怎么点都失败。
    """
    _allow(monkeypatch)
    code = _code_of(project_id)

    resp = client.post(f"/ai-analysis/projects/{project_id}/knowledge/scaffold", json={})

    assert resp.status_code == 200, resp.get_json()
    pack = _pack_of(projects_root, code)
    assert (pack / PROJECT_PACK_MANIFEST).is_file()
    for name in ("config-table-spec.md", "gameplay-semantics.md"):
        assert (pack / "references" / name).is_file(), f"模板引用的 {name} 没有一起建出来"
    assert validate_project_pack(pack) == [], "模板建出来的包不合法，用户第一步就卡住"

    with flask_app.app_context():
        loaded = load_skills(REPO_ROOT, project_code=code)
    assert {document.name for document in loaded.project_references} == {
        "config-table-spec.md",
        "gameplay-semantics.md",
    }


def test_the_template_does_not_overwrite_an_existing_pack(client, project_id, projects_root, monkeypatch):
    """已经有清单时不许覆盖 —— 那会把用户写好的一整份说明冲掉。"""
    _allow(monkeypatch)
    pack = _seed_pack(projects_root, _code_of(project_id))
    before = (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")

    resp = client.post(f"/ai-analysis/projects/{project_id}/knowledge/scaffold", json={})

    assert resp.status_code == 400
    assert (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8") == before


def test_reading_a_missing_file_is_a_readable_rejection_not_a_500(
    client, project_id, projects_root, monkeypatch
):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.get(f"/ai-analysis/projects/{project_id}/knowledge/references/nope")
    assert resp.status_code == 400
    assert "不存在" in resp.get_json()["message"]


def test_every_document_can_be_read_back(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code)
    (pack / "segment-rules").mkdir()
    (pack / "segment-rules" / "SKILL.md").write_text(
        "---\nname: segment-rules\ndescription: 段位规则\n---\n\n# 段位\n", encoding="utf-8"
    )

    for suffix, expected in (
        ("manifest", "description: 项目知识包"),
        ("references/spec", REFERENCE_BODY),
        ("skills/segment-rules", "# 段位\n"),
    ):
        resp = client.get(f"/ai-analysis/projects/{project_id}/knowledge/{suffix}")
        assert resp.status_code == 200, f"{suffix}: {resp.get_json()}"
        assert expected in resp.get_json()["content"]


def test_the_listing_reports_each_kind_with_its_usage(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    pack = _seed_pack(projects_root, _code_of(project_id))
    (pack / "segment-rules").mkdir()
    (pack / "segment-rules" / "SKILL.md").write_text(
        "---\nname: segment-rules\ndescription: 段位规则\n---\n\n# 段位\n", encoding="utf-8"
    )

    body = client.get(f"/ai-analysis/projects/{project_id}/knowledge").get_json()

    by_kind = {entry["kind"]: entry for entry in body["entries"]}
    assert set(by_kind) == {"manifest", "reference", "skill"}
    assert by_kind["manifest"]["removable"] is False, "清单不该可以被删除"
    assert by_kind["reference"]["removable"] is True
    assert by_kind["skill"]["dir_name"] == "segment-rules"
    for entry in body["entries"]:
        # 「它在分析里怎么被用到」必须逐条给出来 —— 用户看不懂这个区别就会乱放内容
        assert entry["usage"], f"{entry['name']} 没有说明它在分析里怎么被用到"
        assert entry["size"] > 0
        assert entry["modified_at"]


# ==========================================================================
# 六、子 skill 的增删，与「引用行同写」这一族
# ==========================================================================


def test_a_new_skill_gets_server_composed_frontmatter(client, project_id, projects_root, monkeypatch):
    """frontmatter 由服务端拼 —— 用户只给目录名与一句话说明。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    _seed_pack(projects_root, code)

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/skills/segment-rules",
        json={"description": "ID 段位的判定规则", "body": "# 段位\n\n10-19 是角色。\n"},
    )
    assert resp.status_code == 200, resp.get_json()

    pack = _pack_of(projects_root, code)
    text = (pack / "segment-rules" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: segment-rules\ndescription: ID 段位的判定规则\n---\n")
    assert validate_project_pack(pack) == []

    with flask_app.app_context():
        loaded = load_skills(REPO_ROOT, project_code=code)
    assert {document.name for document in loaded.project_skills} == {"segment-rules"}


def test_deleting_a_skill_removes_its_whole_directory(client, project_id, projects_root, monkeypatch):
    """删子 skill 要连目录一起删干净 —— 只删 SKILL.md 会留下一个空壳目录。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code)
    skill_dir = pack / "segment-rules"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: segment-rules\ndescription: 段位规则\n---\n\n# 段位\n", encoding="utf-8"
    )
    # 用户在子 skill 目录里另有文件（界面上不暴露，但磁盘上可能有）
    (skill_dir / "notes.md").write_text("备注\n", encoding="utf-8")

    resp = client.delete(f"/ai-analysis/projects/{project_id}/knowledge/skills/segment-rules")

    assert resp.status_code == 200, resp.get_json()
    assert not skill_dir.exists(), "目录还在 —— 文件管理器里会看到一个空壳"
    assert validate_project_pack(pack) == []


def test_creating_a_reference_adds_the_manifest_line_in_the_same_write(
    client, project_id, projects_root, monkeypatch
):
    """**双向校验的必然结果**：新建文档必须同时在清单里补一行引用。

    「先建文档再改清单」和「先改清单再建文档」两种顺序都不成立 —— 第一种被
    「没有被正文提到」拒绝，第二种被「提到了但文件不存在」拒绝。所以补引用行
    必须在同一次写入里发生。
    """
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code, reference=None)

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/new-spec",
        json={"content": "# 新规范\n", "description": "配表规范：ID 段位"},
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["mention_added"] is True
    manifest = (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")
    assert "`new-spec.md`" in manifest, "清单里没有补上引用行"
    assert "配表规范：ID 段位" in manifest, "补的那一行没有带上用户填的说明"
    assert validate_project_pack(pack) == []
    assert "new-spec.md" in {
        document.name
        for document in load_skills(REPO_ROOT, project_code=code).project_references
    }


def test_creating_a_reference_keeps_a_mention_the_user_already_wrote(
    client, project_id, projects_root, monkeypatch
):
    """清单里已经有引用行时不动它 —— 那一行是用户自己的措辞。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code, reference=None)
    (pack / "references").mkdir(exist_ok=True)
    (pack / "references" / "new-spec.md").write_text("# 新规范\n", encoding="utf-8")
    original = (
        "---\nname: pack\ndescription: 包\n---\n\n# 包\n\n- `new-spec.md` —— 我自己写的说明\n"
    )
    assert (
        client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/manifest", json={"content": original}
        ).status_code
        == 200
    )

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/new-spec",
        json={"content": "# 新规范（改过）\n", "description": "会被忽略的说明"},
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["mention_added"] is False
    manifest = (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")
    assert "我自己写的说明" in manifest
    assert "会被忽略的说明" not in manifest


def test_editing_an_existing_document_does_not_touch_the_manifest(
    client, project_id, projects_root, monkeypatch
):
    """改一份已经有引用行的文档：清单一个字都不该变。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code)
    before = (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")

    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/spec",
        json={"content": "# 改过的正文\n", "description": "随手的说明"},
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["mention_added"] is False
    assert (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8") == before
    assert "改过的正文" in (pack / "references" / "spec.md").read_text(encoding="utf-8")


def test_deleting_a_reference_removes_its_manifest_line_in_the_same_write(
    client, project_id, projects_root, monkeypatch
):
    """删除与新建对称：不摘掉引用行的话，「删除」这个动作**永远不可能成功**。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code)

    resp = client.delete(f"/ai-analysis/projects/{project_id}/knowledge/references/spec")

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["mention_removed"] is True
    assert not (pack / "references" / "spec.md").exists()
    assert "spec.md" not in (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")
    assert validate_project_pack(pack) == []


def test_deleting_a_reference_leaves_the_rest_of_the_manifest_alone(
    client, project_id, projects_root, monkeypatch
):
    """只删提到它的那一行，其余内容一个字不动 —— 清单是用户写的。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code, reference=None)
    (pack / "references").mkdir(exist_ok=True)
    for name in ("spec.md", "other.md"):
        (pack / "references" / name).write_text("# 文档\n", encoding="utf-8")
    content = (
        "---\nname: pack\ndescription: 包\n---\n\n# 包\n\n"
        "- `spec.md` —— 要删的那份\n"
        "- `other.md` —— 保留的那份\n"
        "\n## 这一节不能被自动改动\n\n正文内容。\n"
    )
    assert (
        client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/manifest", json={"content": content}
        ).status_code
        == 200
    )

    assert (
        client.delete(f"/ai-analysis/projects/{project_id}/knowledge/references/spec").status_code
        == 200
    )

    manifest = (pack / PROJECT_PACK_MANIFEST).read_text(encoding="utf-8")
    assert "spec.md" not in manifest
    assert "`other.md` —— 保留的那份" in manifest
    assert "## 这一节不能被自动改动" in manifest
    assert "正文内容。" in manifest


def test_creating_a_document_before_the_manifest_gives_an_actionable_message(
    client, project_id, projects_root, monkeypatch
):
    """空目录 → 直接建文档：要告诉用户「先建清单」，而不是回一句绝对路径。"""
    _allow(monkeypatch)
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/spec",
        json={"content": REFERENCE_BODY, "description": "说明"},
    )
    assert resp.status_code == 400
    message = resp.get_json()["message"]
    assert "KNOWLEDGE.md" in message
    assert "从模板创建" in message, "只说「不行」等于没说"


def test_the_manifest_cannot_be_deleted(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    pack = _seed_pack(projects_root, _code_of(project_id))

    resp = client.delete(f"/ai-analysis/projects/{project_id}/knowledge/manifest")

    assert resp.status_code == 400
    assert "不能删除" in resp.get_json()["message"]
    assert (pack / PROJECT_PACK_MANIFEST).is_file()


# ==========================================================================
# 七、权限
# ==========================================================================

KNOWLEDGE_READ_URLS = (
    "/knowledge",
    "/knowledge/manifest",
    "/knowledge/references/spec",
    "/knowledge/skills/segment-rules",
)
KNOWLEDGE_WRITE_REQUESTS = (
    ("post", "/knowledge/scaffold"),
    ("put", "/knowledge/manifest"),
    ("delete", "/knowledge/manifest"),
    ("put", "/knowledge/references/spec"),
    ("delete", "/knowledge/references/spec"),
    ("put", "/knowledge/skills/segment-rules"),
    ("delete", "/knowledge/skills/segment-rules"),
)
_WRITE_BODY = {"content": "# x\n", "description": "d", "body": "# x\n"}


def test_reading_requires_project_access(client, project_id, monkeypatch):
    _allow(monkeypatch, access=False, admin=False)
    for suffix in KNOWLEDGE_READ_URLS:
        resp = client.get(f"/ai-analysis/projects/{project_id}{suffix}")
        assert resp.status_code == 403, f"{suffix} 返回了 {resp.status_code}"


def test_writing_requires_project_admin(client, project_id, monkeypatch):
    """**全部写端点逐一确认**：明确点名而不是数个数。

    数数字的断言在新增端点时会无意义地变红（于是有人把数字改掉就完事），
    点名断言只在「某处判定被删」或「新增端点忘了判」时变红 —— 那才是我们想知道的。
    """
    _allow(monkeypatch, access=True, admin=False)
    for method, suffix in KNOWLEDGE_WRITE_REQUESTS:
        resp = getattr(client, method)(
            f"/ai-analysis/projects/{project_id}{suffix}", json=_WRITE_BODY
        )
        assert resp.status_code == 403, f"{method.upper()} {suffix} 返回了 {resp.status_code}"
        assert resp.get_json()["success"] is False


def test_a_denied_write_never_touches_the_disk(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch, access=True, admin=False)
    code = _code_of(project_id)
    pack = _seed_pack(projects_root, code)
    before = _snapshot(projects_root)

    for method, suffix in KNOWLEDGE_WRITE_REQUESTS:
        getattr(client, method)(f"/ai-analysis/projects/{project_id}{suffix}", json=_WRITE_BODY)

    assert _snapshot(projects_root) == before
    assert (pack / PROJECT_PACK_MANIFEST).is_file()


def test_a_denied_request_is_a_403_not_a_redirect(client, project_id, monkeypatch):
    """403 而不是 302 跳登录页：跳转会让前端拿到一张 HTML、报「解析失败」。"""
    _allow(monkeypatch, access=False, admin=False)
    for method, suffix in KNOWLEDGE_WRITE_REQUESTS + tuple(
        ("get", url) for url in KNOWLEDGE_READ_URLS
    ):
        resp = getattr(client, method)(
            f"/ai-analysis/projects/{project_id}{suffix}", json=_WRITE_BODY
        )
        assert resp.status_code in (401, 403), f"{method.upper()} {suffix} 返回了 {resp.status_code}"


def test_the_routes_use_the_same_permission_helpers_as_the_config_endpoint():
    """口径必须与 `/config` 那条一模一样，不另立一套。

    写侧用 `_has_project_admin_access`、读侧用 `_has_project_access` ——
    与配置的 POST / GET 逐字一致。改这里的判定就等于改了一个安全边界，
    所以用一条静态断言把它钉住。
    """
    source = (REPO_ROOT / "routes" / "ai_analysis_routes.py").read_text(encoding="utf-8")
    read_helper = source[source.index("def _knowledge_read(") : source.index("def _knowledge_write(")]
    write_helper = source[
        source.index("def _knowledge_write(") : source.index("def _knowledge_error_response(")
    ]
    assert "_has_project_access(project_id)" in read_helper
    assert "_has_project_admin_access(project_id)" in write_helper
    assert "403" in read_helper and "403" in write_helper


# ==========================================================================
# 八、溯源：保存之后 skill_revision 真的变了
# ==========================================================================


def test_saving_changes_the_skill_revision(client, project_id, projects_root, monkeypatch):
    """改完知识包，`load_skills(...).revision` 必须变。

    它进 run 的 `skill_version` 溯源字段，也是「旧结论还算不算数」的判据之一。
    不变的话，用户改了知识包却拿到按旧规则跑出来的结论，而界面上一切正常。
    """
    _allow(monkeypatch)
    code = _code_of(project_id)
    _seed_pack(projects_root, code)

    with flask_app.app_context():
        before = load_skills(REPO_ROOT, project_code=code).revision

    assert (
        client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/references/spec",
            json={"content": "# 配表规范（改过）\n\nID 为 6 位，前两位是类型段。\n"},
        ).status_code
        == 200
    )
    with flask_app.app_context():
        after = load_skills(REPO_ROOT, project_code=code).revision

    assert after != before, "知识包改了但 revision 没变 —— 历史结论会被当成新结论复用"


def test_creating_the_pack_from_scratch_also_changes_the_revision(
    client, project_id, projects_root, monkeypatch
):
    """从「没有知识包」到「有」，revision 也必须变（原来那份索引里什么都没有）。"""
    _allow(monkeypatch)
    code = _code_of(project_id)
    with flask_app.app_context():
        before = load_skills(REPO_ROOT, project_code=code).revision

    assert (
        client.post(f"/ai-analysis/projects/{project_id}/knowledge/scaffold", json={}).status_code
        == 200
    )

    with flask_app.app_context():
        after = load_skills(REPO_ROOT, project_code=code).revision
    assert after != before


def test_deleting_a_document_also_changes_the_revision(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    code = _code_of(project_id)
    _seed_pack(projects_root, code)
    with flask_app.app_context():
        before = load_skills(REPO_ROOT, project_code=code).revision

    assert (
        client.delete(
            f"/ai-analysis/projects/{project_id}/knowledge/references/spec"
        ).status_code
        == 200
    )

    with flask_app.app_context():
        after = load_skills(REPO_ROOT, project_code=code).revision
    assert after != before


# ==========================================================================
# 九、归因：错误落到具体文件 / 具体栏
# ==========================================================================


def test_problems_about_other_files_are_labelled_with_that_file():
    """说的是别的文件时，label 要点明是哪一份 —— 用户才知道去哪一栏改。"""
    errors = project_pack_service._attribute_problems(
        ["references/spec.md 没有被正文提到，模型不会知道它可读"],
        edited_rel="references/other.md",
    )
    assert errors[0].field == "__pack__"
    assert errors[0].label == "references/spec.md"


def test_problems_about_a_sub_skill_are_labelled_with_its_directory():
    errors = project_pack_service._attribute_problems(
        ["segment-rules/：SKILL.md 超过 500 行，应拆到 references/"],
        edited_rel=PROJECT_PACK_MANIFEST,
    )
    assert errors[0].field == "__pack__"
    assert errors[0].label == "segment-rules/SKILL.md"


def test_problems_about_the_edited_file_land_on_its_own_editor():
    errors = project_pack_service._attribute_problems(
        ["KNOWLEDGE.md: frontmatter 只有开始标记 `---`，没有结束标记"],
        edited_rel=PROJECT_PACK_MANIFEST,
    )
    assert errors[0].field == "content"
    assert errors[0].message == "frontmatter 只有开始标记 `---`，没有结束标记"


def test_a_long_reference_without_a_toc_is_attributed_to_that_reference():
    errors = project_pack_service._attribute_problems(
        ["huge.md 超过 300 行却没有目录（skill-creator 要求长 reference 带 TOC）"],
        edited_rel=PROJECT_PACK_MANIFEST,
    )
    assert errors[0].field == "__pack__"
    assert errors[0].label == "references/huge.md"


def test_an_unattributable_problem_is_never_dropped():
    """归因可以笼统，但**不能把问题丢掉** —— 丢掉就等于「保存失败但没说为什么」。"""
    errors = project_pack_service._attribute_problems(["一句谁也认不出来的问题"], edited_rel="")
    assert len(errors) == 1
    assert errors[0].message == "一句谁也认不出来的问题"


# ==========================================================================
# 十、请求体形状与响应契约
# ==========================================================================


def test_a_non_object_body_is_rejected_with_400(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/spec",
        json=["not", "an", "object"],
    )
    assert resp.status_code == 400


def test_every_error_response_carries_an_errors_list(client, project_id, projects_root, monkeypatch):
    """`errors` 一定存在（哪怕是空的）—— 前端读它时不必先判存在。

    少一个分支带 `errors`，「字段级明细」就会在那个分支上静默降级成「只有一个 toast」。
    """
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    responses = [
        client.get(f"/ai-analysis/projects/{project_id}/knowledge/references/nope"),
        client.delete(f"/ai-analysis/projects/{project_id}/knowledge/manifest"),
        client.put(
            f"/ai-analysis/projects/{project_id}/knowledge/skills/segment-rules",
            json={"description": "", "body": ""},
        ),
    ]
    for resp in responses:
        body = resp.get_json()
        assert resp.status_code == 400, body
        assert isinstance(body["errors"], list)
        assert body["errors"], f"没有明细：{body}"
        assert body["message"], "没有一句人能读的总结"


def test_a_failure_message_names_the_file_and_the_reason(client, project_id, projects_root, monkeypatch):
    """错误要能照着做：写清是哪一份文件、为什么不行。"""
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    resp = client.put(
        f"/ai-analysis/projects/{project_id}/knowledge/references/UPPER",
        json={"content": "# x\n"},
    )
    message = resp.get_json()["message"]
    assert "文件名" in message
    assert "小写字母" in message


def test_the_whole_response_is_json_serialisable(client, project_id, projects_root, monkeypatch):
    _allow(monkeypatch)
    _seed_pack(projects_root, _code_of(project_id))
    raw = client.get(f"/ai-analysis/projects/{project_id}/knowledge").get_data(as_text=True)
    json.loads(raw)


def test_a_project_without_a_code_is_refused_with_an_actionable_message(
    client, project_id, projects_root, monkeypatch
):
    _allow(monkeypatch)
    _set_code(project_id, "")

    resp = client.get(f"/ai-analysis/projects/{project_id}/knowledge")
    assert resp.status_code == 400
    assert "项目代号" in resp.get_json()["message"]


def test_a_missing_project_is_a_404_not_a_500(client, projects_root, monkeypatch):
    _allow(monkeypatch)
    resp = client.get("/ai-analysis/projects/999999/knowledge")
    assert resp.status_code == 404


def test_reading_never_creates_the_pack_directory(projects_root, monkeypatch):
    """`describe_pack` 只读 —— 只是打开面板看一眼，磁盘上不该多出东西。"""
    monkeypatch.setenv(SKILL_PROJECTS_ROOT_ENV, str(projects_root))
    project_pack_service.describe_pack("g999")
    assert os.listdir(projects_root) == []
