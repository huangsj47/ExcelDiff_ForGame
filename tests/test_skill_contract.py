"""skill 的结构契约必须被守住。

## 为什么值得一组测试

`SKILL.md` 同时被三方读取：**平台运行期**（注入提示词）、**测试**、以及
`skill-creator` 的校验器。三方对「合法」的判定一旦不一致，后果都是静默的：

- 运行期的 `DIMENSION_IDS` 与文档里的 `category` 枚举不同步 → 模型输出服务端不认的
  类别，异常被**静默丢弃**，报告看起来正常但少了一批结论。
- 报告章节与 `REPORT_SECTIONS` 不同步 → 「轮次耗尽但回答像报告」的降级判定永远不成立，
  本该降级返回的报告变成整单失败。
- `references/` 里有文件但正文没列 → 那份文档**永远不会被读到**（渐进式披露的入口
  就是正文里那张表），而模型不知道自己错过了什么。

这些都不会报错，只会让功能悄悄变差，所以用测试钉住。

## 反向自检是必须的

只写「当前 skill 合法」是不够的：校验器被改坏（比如返回空列表）时它照样全绿。
下面每条检查都配了一个**必须判红**的用例，与仓库里 `test_a11y_icon_target_size.py`
的 `test_the_measurement_model_is_not_vacuous` 是同一思路。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from services.ai.skill_contract import (
    DIMENSION_IDS,
    PLATFORM_SKILL_RELATIVE_PATH,
    REPORT_SECTIONS,
    SKILL_MD_MAX_LINES,
    SkillContractError,
    _mentioned_markdown_files,
    extract_category_enum,
    extract_report_sections,
    parse_frontmatter,
    validate_all,
    validate_project_pack,
    validate_skill_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_SKILL_DIR = REPO_ROOT / PLATFORM_SKILL_RELATIVE_PATH

# skill-creator 是作者本机的工具，不在仓库里。存在时做一次交叉校验，
# 不存在就跳过——CI 里必然跳过，所以它只提供本地的额外保障。
SKILL_CREATOR_DIR = Path.home() / ".claude" / "skills" / "skill-creator"


def _valid_pack_body(description: str = "一个用于测试的项目知识包") -> str:
    return (
        "---\n"
        "name: sample-pack\n"
        f"description: {description}\n"
        "---\n"
        "\n"
        "# 示例包\n"
        "\n"
        "读 `references/a.md` 了解详情。\n"
    )


def _make_pack(tmp_path: Path, body: str, references: tuple[str, ...] = ("a.md",)) -> Path:
    pack = tmp_path / "sample-pack"
    (pack / "references").mkdir(parents=True)
    (pack / "KNOWLEDGE.md").write_text(body, encoding="utf-8")
    for name in references:
        (pack / "references" / name).write_text("# 文档\n", encoding="utf-8")
    return pack


# --------------------------------------------------------------------------
# 当前仓库的 skill 满足契约
# --------------------------------------------------------------------------


def test_shipped_skills_satisfy_the_contract():
    """平台 skill 与全部项目知识包都要通过校验。"""
    failures = validate_all(REPO_ROOT)
    assert failures == {}, "以下 skill 不符合契约：\n" + "\n".join(
        f"  {path}:\n" + "\n".join(f"    - {item}" for item in problems)
        for path, problems in failures.items()
    )


def test_skill_md_stays_within_the_line_budget():
    """SKILL.md 超出预算时该拆到 references/，而不是继续堆。"""
    lines = len((PLATFORM_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").splitlines())
    assert lines <= SKILL_MD_MAX_LINES, (
        f"SKILL.md 已 {lines} 行，上限 {SKILL_MD_MAX_LINES} 行 —— 把细节拆到 references/，"
        "并在正文里写明何时读它"
    )


# --------------------------------------------------------------------------
# 跨模块契约：文档里的枚举必须等于运行期常量
# --------------------------------------------------------------------------


def _platform_body() -> str:
    _fields, body = parse_frontmatter((PLATFORM_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    return body


def test_category_enum_in_the_doc_equals_the_runtime_dimension_ids():
    """模型看到的 `category` 枚举，必须与运行期校验用的是同一组值。

    不同步的后果是静默的：模型输出一个服务端不认的 category，那条异常会被丢掉，
    而报告里看不出少了东西。
    """
    assert extract_category_enum(_platform_body()) == DIMENSION_IDS


def test_report_sections_in_the_doc_equal_the_runtime_report_sections():
    """报告章节必须与降级判定用的标题清单一致。"""
    assert extract_report_sections(_platform_body()) == REPORT_SECTIONS


def test_the_shipped_skill_requires_coupling_analysis_and_test_risks():
    """耦合分析是**内置提示词**的一部分，不是可选的加分项。

    用户要的是「分析模块之间的耦合状态，有耦合内容需提示相关测试风险点」。
    这三条缺一条，落地的效果就变了：

    * 少了「必须给出测试风险点」→ 报告会写一句「这里存在耦合，注意回归」就完事，
      QA 拿不到可执行的用例；
    * 少了「本该成对改动的两边只改了一边」→ 最值钱的那类信号（改表没导表、
      改客户端没改服务端）根本不会被找出来；
    * 少了「读不到另一端时写成待确认」→ 模型会凭文件名相似断言另一端有问题，
      而那正是这个 skill 反复强调要避免的过度归因。

    2026-09-24：最后一条断言原先钉的是「你只能读到**本批次改动过的文件**」。那句话当时
    就已经与同一份文件的三处（分工模式的「读取权限是整个冻结版本」、`file_content` 的
    「冻结版本上任何一个 Git 跟踪文件」、`find_references` 的「范围是这一版的仓库而不是
    本批次的文件」）**自相矛盾**，而模型两种都读到过（run 57 的 7 条误拒里 4 条正是按
    前一句理解的结果）。现在钉的是**真正的边界**：读不到只有那几种原因，以及「读到的是
    背景版本、不能当本次改动的证据」这一条 —— 后者是新的误归因入口（当前 tip 的内容
    被写成「本次改了它」）。
    """
    body = _platform_body()

    assert "就必须给出与之对应的测试风险点" in body, "没有把「耦合 → 测试风险点」写成硬要求"
    assert "本该成对改动的两边只改了一边" in body, "没有点出「只改一半」这类信号"
    assert "待确认的耦合点" in body, "没有规定「读不到另一端」时该怎么写"
    assert "不在本项目任何已接入仓库的跟踪树里" in body, (
        "没有讲清建模**真正读不到**的是什么（凭文件名相似断言耦合，源头就是这里含糊）"
    )
    assert "不是本次那一条提交上的" in body, (
        "没有讲清 `file_content` / `find_references` 给的是当前 tip 而不是本次那条提交，"
        "背景版本会被当成「本次改了它」的证据"
    )


def test_the_skill_requires_a_checkable_information_gap():
    """「信息缺口」必须写成能核对的一条：缺哪个文件的什么、为什么取不到、挡住了哪个维度。

    **为什么这条值得钉**（2026-09-19，一次真实报告逼出来的）：报告里写着

        信息缺口（谨慎使用）：本报告未读到任何代码 diff 与协议 diff，code_logic、
        version_branch 两个维度无法判断。

    ——没有文件、没有失败原因。复核的人拿到它，既不知道是哪个文件没读到，也不知道该去
    查什么（Agent 离线？同步没跑完？还是这个提交里确实没有这个路径？三件事的处理方式
    完全不同）。而工具**已经把原因写在返回内容里了**（「平台读不到 X」/「已向 Agent
    索取，15 秒内没等到」/「项目没有绑定 Agent 节点」），照抄即可。

    另外「取不到」与「确实没有内容」在这个协议里是两件事（`None` 与 `""`），合并写会
    把「没有证据」读成「这里没问题」——那正是这个 skill 存在的理由。
    """
    body = _platform_body()

    assert "写成能被人拿去核对的一条" in body, "没有要求把信息缺口写成可核对的一条"
    assert "缺的是哪个文件的什么" in body, "没有要求在缺口里点名文件与索取类型"
    assert "原话" in body, "没有要求照抄工具给的失败原因（改写成「未取到」就丢了线索）"
    assert "不许合并写" in body, "没有把「取不到」与「确实没有内容」分开"


def test_every_dimension_id_has_a_section_explaining_it():
    """枚举里列的每个维度，正文都要有一节讲「这个维度要查什么」。

    **为什么这条值得单独钉**：枚举与正文是两套东西。只在枚举里加一个 id、忘了写正文，
    模型就会知道有这么个维度、却不知道要查什么 —— 而它**照样会按枚举填 `dimensions`**，
    于是那一项永远是 `hit: false` 加一句勉强的理由。报告看起来「每个维度都过了一遍」，
    实际上那个维度从来没被真正检查过，而且没有任何报错。

    顺带要求编号连续：`### 1.`…`### N.` 中间漏号或重号，读的人会以为少了一节。
    """
    numbers = [int(n) for n in re.findall(r"^### (\d+)\. `", _platform_body(), flags=re.M)]
    assert len(numbers) == len(DIMENSION_IDS), (
        f"正文里的维度小节有 {len(numbers)} 节，枚举里是 {len(DIMENSION_IDS)} 个维度"
    )
    assert numbers == list(range(1, len(numbers) + 1)), f"维度小节编号不连续：{numbers}"


def test_the_dimension_section_ids_match_the_runtime_ids():
    """正文小节标题里的 id 必须与运行期常量同一组（顺序不同不算错，缺一个才算）。"""
    declared = set(re.findall(r"^### \d+\. `([a-z_]+)`", _platform_body(), flags=re.M))
    assert declared == set(DIMENSION_IDS), (
        f"正文讲了但枚举里没有：{sorted(declared - set(DIMENSION_IDS))}；"
        f"枚举里有但正文没讲：{sorted(set(DIMENSION_IDS) - declared)}"
    )


def test_the_enum_extractors_actually_read_the_document():
    """反向自检：提取器不能是「恒返回期望值」。

    用一个**明显不同**的文档去调，必须拿到文档里的值而不是常量。
    """
    assert extract_category_enum('{"category": "alpha | beta"}') == ("alpha", "beta")

    # 报告章节的提取要求「代码块里包含全部章节」才算数（否则正文里随便一处 `# `
    # 行都会被误当成章节清单）。所以自检给一个**顺序被打乱**的完整清单：
    # 提取器必须返回文档里的顺序，而不是把常量原样吐出来。
    shuffled = tuple(reversed(REPORT_SECTIONS))
    block = "```\n" + "\n".join(f"# {name}" for name in shuffled) + "\n```\n"
    assert extract_report_sections(block) == shuffled

    with pytest.raises(SkillContractError):
        extract_category_enum("文档里没有这个字段")
    with pytest.raises(SkillContractError):
        extract_report_sections("文档里没有章节清单")
    with pytest.raises(SkillContractError):
        # 只给部分章节时必须拒绝，否则会把正文里的片段当成报告结构。
        extract_report_sections("```\n# 变更理解\n# 风险评估\n```\n")


# --------------------------------------------------------------------------
# frontmatter 解析器：接受什么、拒绝什么
# --------------------------------------------------------------------------


def test_frontmatter_accepts_the_shipped_shape():
    fields, body = parse_frontmatter(_valid_pack_body())
    assert fields == {"name": "sample-pack", "description": "一个用于测试的项目知识包"}
    assert body.lstrip().startswith("# 示例包")


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("没有 frontmatter", "# 只有正文\n"),
        ("前面有空行", "\n---\nname: a\ndescription: b\n---\n"),
        ("没有结束标记", "---\nname: a\ndescription: b\n"),
        ("有注释", "---\n# 注释\nname: a\ndescription: b\n---\n"),
        ("缺冒号", "---\nname a\n---\n"),
        ("值为空", "---\nname:\ndescription: b\n---\n"),
        ("键重复", "---\nname: a\nname: b\ndescription: c\n---\n"),
        # 值里含 `: ` 时 YAML 会把它当成新的键值对 —— 我们不用 YAML，但
        # skill-creator 的校验器用，所以这类写法必须在这里就挡住。
        ("值里含冒号加空格", "---\nname: a\ndescription: 前半: 后半\n---\n"),
        ("值里含注释引导", "---\nname: a\ndescription: 前半 # 后半\n---\n"),
        ("值以引号开头", '---\nname: a\ndescription: "带引号"\n---\n'),
        ("值以方括号开头", "---\nname: a\ndescription: [列表]\n---\n"),
    ],
)
def test_frontmatter_rejects_shapes_it_cannot_parse_identically_to_yaml(label, text):
    """解析器只接受最简单的 `key: value`，其余一律拒绝而不是猜。"""
    with pytest.raises(SkillContractError):
        parse_frontmatter(text)


# --------------------------------------------------------------------------
# references 索引：双向一致，且围栏不会让它失效
# --------------------------------------------------------------------------


def test_reference_mentioned_but_missing_is_reported(tmp_path):
    pack = _make_pack(tmp_path, _valid_pack_body(), references=())
    problems = validate_project_pack(pack)
    assert any("references/ 下没有这个文件" in item for item in problems), problems


def test_reference_on_disk_but_not_mentioned_is_reported(tmp_path):
    """文件存在但正文没列 → 模型不知道它可读，这份文档等于不存在。"""
    pack = _make_pack(tmp_path, _valid_pack_body(), references=("a.md", "orphan.md"))
    problems = validate_project_pack(pack)
    assert any("orphan.md" in item and "没有被正文提到" in item for item in problems), problems


def test_a_clean_pack_passes(tmp_path):
    """反向自检的前提：合规的包必须通过，否则上面的判红可能是别的原因导致的。"""
    pack = _make_pack(tmp_path, _valid_pack_body(), references=("a.md",))
    assert validate_project_pack(pack) == []


def test_fenced_code_blocks_do_not_break_backtick_pairing():
    """含代码围栏的文档里，行内代码的 `.md` 提及必须仍能被提取到。

    **这是一个真实踩到的坑**：围栏是三个反引号，朴素的 `` `([^`]+)` `` 会把「围栏的
    第 3 个反引号」和「闭合围栏的第 1 个反引号」配成一对，导致**其后所有行内代码的
    配对整体错位**。含一段 ```json 骨架的 SKILL.md 因此会让**全部** references 都被
    判成「没被正文提到」——校验器从「守住契约」变成了「无差别判红」。
    """
    body = "前 `references/a.md` 后\n```json\n{\"k\": \"v\"}\n```\n尾 `references/b.md`"
    assert _mentioned_markdown_files(body) == {"a.md", "b.md"}


def test_the_fence_stripping_test_would_fail_without_stripping():
    """反向自检：确认上一条测的确实是「剥围栏」这件事。

    手工按未剥围栏的方式配对一次，证明不解的结果与期望不同 —— 否则上一条可能在
    围栏恰好没有影响配对的巧合下通过。
    """
    import re

    body = "前 `references/a.md` 后\n```json\n{\"k\": \"v\"}\n```\n尾 `references/b.md`"
    naive = {
        Path(span).name
        for span in re.findall(r"`([^`]+)`", body)
        if span.strip().endswith(".md")
    }
    assert naive != {"a.md", "b.md"}, "围栏没有影响朴素配对，这条自检失去意义"


# --------------------------------------------------------------------------
# 其它契约
# --------------------------------------------------------------------------


def test_unknown_frontmatter_key_is_reported(tmp_path):
    body = _valid_pack_body().replace("---\n\n# 示例包", "custom-key: x\n---\n\n# 示例包")
    pack = _make_pack(tmp_path, body)
    problems = validate_project_pack(pack)
    assert any("未允许的键" in item and "custom-key" in item for item in problems), problems


def test_skill_without_any_entry_file_is_reported(tmp_path):
    empty = tmp_path / "empty-skill"
    empty.mkdir()
    assert validate_skill_dir(empty) != []


@pytest.mark.skipif(
    not (SKILL_CREATOR_DIR / "scripts" / "quick_validate.py").is_file(),
    reason="本机没有 skill-creator，跳过交叉校验（CI 里必然跳过）",
)
def test_platform_skill_also_passes_skill_creators_own_validator():
    """与 skill-creator 自带的校验器交叉验证。

    我们的解析器与它的 PyYAML 判定如果有分歧，这条会先发现。
    """
    result = subprocess.run(
        [sys.executable, "scripts/quick_validate.py", str(PLATFORM_SKILL_DIR)],
        cwd=SKILL_CREATOR_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
