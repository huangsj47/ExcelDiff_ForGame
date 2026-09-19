import json
import uuid
from datetime import datetime, timedelta, timezone

import services.ai.provenance as provenance
import services.ai_analysis_service as ai_service
from app import app, create_tables, db
from models import Project, Repository, WeeklyVersionConfig, WeeklyVersionDiffCache
from models.ai_analysis import AiAnalysisRun, AiProjectApiKey, AiWeeklyAnalysisState


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_project():
    project = Project(code=_uid("P"), name=_uid("ai-project"))
    db.session.add(project)
    db.session.flush()
    return project


def _create_repo(project_id: int, name: str, repo_type: str, resource_type: str) -> Repository:
    repo = Repository(
        project_id=project_id,
        name=name,
        type=repo_type,
        url=f"https://example.com/{name}.git",
        branch="main",
        resource_type=resource_type,
        clone_status="completed",
    )
    db.session.add(repo)
    db.session.flush()
    return repo


def _create_weekly_config(project_id: int, repo: Repository, base_name: str, start_time, end_time):
    cfg = WeeklyVersionConfig(
        project_id=project_id,
        repository_id=repo.id,
        name=f"{base_name} - {repo.name}",
        description="",
        branch="main",
        start_time=start_time,
        end_time=end_time,
        cycle_type="custom",
        is_active=True,
        auto_sync=True,
        status="active",
    )
    db.session.add(cfg)
    db.session.flush()
    return cfg


def _seed_diff_cache(
    config: WeeklyVersionConfig,
    repo: Repository,
    path: str,
    updated_at: datetime,
    *,
    commit_id: str | None = None,
):
    cache = WeeklyVersionDiffCache(
        config_id=config.id,
        repository_id=repo.id,
        file_path=path,
        file_type="code",
        latest_commit_id=commit_id or _uid("c"),
        commit_count=1,
        updated_at=updated_at,
    )
    db.session.add(cache)


def test_ai_weekly_payload_scope_and_policy():
    with app.app_context():
        create_tables()
        project = _create_project()
        repo_code = _create_repo(project.id, _uid("code"), "git", "code")
        repo_table = _create_repo(project.id, _uid("table"), "svn", "table")

        start_time = datetime(2026, 3, 1, 0, 0)
        end_time = datetime(2026, 3, 8, 0, 0)

        cfg_code = _create_weekly_config(project.id, repo_code, "W1", start_time, end_time)
        cfg_table = _create_weekly_config(project.id, repo_table, "W1", start_time, end_time)

        base_time = datetime.now(timezone.utc) - timedelta(hours=2)
        for i in range(5):
            _seed_diff_cache(cfg_code, repo_code, f"src/file_{i}.py", base_time)
            _seed_diff_cache(cfg_table, repo_table, f"data/file_{i}.csv", base_time)
        db.session.commit()

        payload, state, skip_reason = ai_service.build_weekly_payload(cfg_code.id)
        assert skip_reason is None
        assert payload["scope"] == "full"
        assert payload["execution"]["version"] == "latest"
        assert payload["policy"]["allow_cross_file"] is True
        assert payload["repositories"][0]["repository_id"] == repo_code.id

        last_analyzed_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        group_key = ai_service.build_weekly_group_key(cfg_code)
        state = AiWeeklyAnalysisState(
            project_id=project.id,
            group_key=group_key,
            base_name="W1",
            start_time=start_time,
            end_time=end_time,
            last_analyzed_at=last_analyzed_at,
        )
        db.session.add(state)
        db.session.commit()

        updated_entry = WeeklyVersionDiffCache.query.filter_by(config_id=cfg_code.id).first()
        updated_entry.updated_at = datetime.now(timezone.utc)
        db.session.commit()

        payload, state, skip_reason = ai_service.build_weekly_payload(cfg_code.id)
        assert skip_reason is None
        assert payload["scope"] == "incremental"
        assert payload["policy"]["reason"] == "delta_small"


def test_the_default_prompt_is_project_agnostic():
    """平台默认提示词**不能写死某个项目的技术栈**。

    旧文案是「当前游戏基于 Unity 引擎开发，使用 C# 与 Lua 脚本语言，类型为 FPS 射击游戏」。
    这是**平台级**默认值：接入任何一个别的项目，模型都会被告知这是 G119 的技术栈，
    于是按 FPS 的常识去判断改动的影响面。

    旧文案还有第二个病：「请基于以下**变更 diff** 与提交信息输出」，而当时的 payload
    里根本没有 diff 内容 —— 要求模型基于它拿不到的东西作答。

    现在技术栈只作为**示例**出现在模板里，用户按自己项目的情况填。
    """
    from services.ai_analysis_service import DEFAULT_PROMPT_TEMPLATE as tpl

    for banned in ("FPS", "射击", "请基于以下变更 diff"):
        assert banned not in tpl, f"默认提示词里又出现了写死的项目事实：{banned}"

    # 技术栈只以示例形式出现，且要求用户填自己项目的情况
    assert "Unity + C# + Lua" in tpl, "技术栈示例不见了 —— 用户会不知道该填什么格式"
    assert "例：" in tpl
    # 必须交代「没把握就留空」：否则模型会把信息缺口编成事实
    assert "不要编造" in tpl
    # 内置协议是平台强制的，不能被这里的补充指令放宽
    assert "不要在这里重复" in tpl
    assert "放宽" in tpl


def test_an_empty_prompt_template_stays_empty():
    """用户清空补充指令 = 真的清空，不是被写回默认值。

    旧实现在空值时写回 `DEFAULT_PROMPT_TEMPLATE`：用户想清空却得到一坨文本，
    而且看不出来那不是自己写的。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ai_service.update_project_analysis_config(
            project.id, {"prompt_template": "自定义"}, updated_by="tester"
        )
        ok, _message, errors = ai_service.update_project_analysis_config(
            project.id, {"prompt_template": ""}, updated_by="tester"
        )
        assert ok is True
        assert errors == []
        assert ai_service.get_project_analysis_config(project.id)["prompt_template"] == ""


def test_ai_project_api_key_uses_cross_platform_encryption():
    """**Token 必须走跨平台的 Fernet，不再用 Windows-only 的 DPAPI。**

    原来这条测试把 `encrypt_dpapi` / `decrypt_dpapi` 都 monkeypatch 掉了，于是它只验证了
    「有人把密文写进了库」，**从未验证过真实的加解密链路**，也从未覆盖
    `_get_project_api_key` —— 而那个函数正是 DPAPI 在 Linux 上必然抛 `RuntimeError` 的
    地方（也就是说这个功能在 Linux 部署上从来没能用过）。

    现在不再 mock：真实加密、真实解密、真实回读明文，并断言密文里不含明文。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ok, message = ai_service.set_project_api_key(
            project.id, "secret-key-1234", updated_by="tester"
        )
        assert ok is True
        assert "updated" in message.lower()

        record = AiProjectApiKey.query.filter_by(project_id=project.id).first()
        assert record is not None
        assert record.encrypted_key.startswith("enc::"), "应使用跨平台的 Fernet 前缀"
        assert "secret-key-1234" not in record.encrypted_key, "密文里不能出现明文"

        assert ai_service._get_project_api_key(project.id) == "secret-key-1234"

        status = ai_service.get_project_api_key_status(project.id)
        assert status["configured"] is True
        assert status["updated_at"]
        assert status["format"] == "fernet"


def test_an_empty_api_key_is_rejected():
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ok, message = ai_service.set_project_api_key(project.id, "   ")
        assert ok is False
        assert "empty" in message.lower()
        assert AiProjectApiKey.query.filter_by(project_id=project.id).first() is None


def test_a_legacy_dpapi_ciphertext_does_not_raise():
    """旧的 `dpapi::` 密文要能兼容读：取不到就返回 None 并给出可读指引，**不能抛异常**。

    不兼容读的后果是「已经配过密钥的项目突然全部失效」；抛异常的后果是用户看到一句
    `DPAPI is only available on Windows.`，完全不知道该怎么办。

    这条在 Windows 与 Linux 上都是稳定的：本机 DPAPI 解不开这段伪造密文而返回 None，
    CI 的 Linux 上根本走不到解密那一步 —— 两种情况的结论都是「取不到 + 不抛」。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.add(
            AiProjectApiKey(
                project_id=project.id, encrypted_key="dpapi::not-a-real-ciphertext"
            )
        )
        db.session.commit()

        assert ai_service._get_project_api_key(project.id) is None
        assert ai_service.get_project_api_key_status(project.id)["format"] == "dpapi"


def test_an_unknown_key_format_is_tolerated():
    """库里存了既不是 enc:: 也不是 dpapi:: 的东西时，按 Fernet 路径尝试，不炸。"""
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.add(AiProjectApiKey(project_id=project.id, encrypted_key="plain-garbage"))
        db.session.commit()

        assert ai_service._get_project_api_key(project.id) == "plain-garbage"


def test_ai_project_analysis_config_update():
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        config = ai_service.get_project_analysis_config(project.id)
        assert config["configured"] is False
        assert config["weekly_interval_minutes"] == 60
        # 界面用的字段元信息从接口来，不在模板里写死。
        assert config["field_schema"]["max_analysis_rounds"]["min"] == 1
        assert config["field_schema"]["max_analysis_rounds"]["max"] == 30
        assert config["api_key"]["configured"] is False
        assert config["endpoint_ready"] is False, "地址/模型/Token 都没配，不具备跑分析的条件"

        ok, message, errors = ai_service.update_project_analysis_config(
            project.id,
            {
                "weekly_interval_minutes": 15,
                "auto_weekly_enabled": False,
                "max_files_per_run": 150,
                "prompt_template": "test prompt",
            },
            updated_by="tester",
        )
        assert ok is True
        assert errors == []

        updated = ai_service.get_project_analysis_config(project.id)
        assert updated["configured"] is True
        assert updated["weekly_interval_minutes"] == 15
        assert updated["auto_weekly_enabled"] is False
        assert updated["max_files_per_run"] == 150
        assert updated["prompt_template"] == "test prompt"


def test_an_out_of_range_value_is_rejected_and_nothing_is_written():
    """**越界不夹取、不改库、原值保持。**

    旧实现用 `_clamp_int`：填 5000 会被悄悄存成 1000，用户看到「保存成功」却不知道自己的
    值被改了。现在要返回字段级错误，并保证库里没有任何变化。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ok, _message, _errors = ai_service.update_project_analysis_config(
            project.id, {"weekly_interval_minutes": 15}
        )
        assert ok is True

        ok, _message, errors = ai_service.update_project_analysis_config(
            project.id, {"max_analysis_rounds": 999, "min_severity": "medium"}
        )
        assert ok is False
        fields = {item["field"] for item in errors}
        assert fields == {"max_analysis_rounds", "min_severity"}
        assert all(item["label"] and item["message"] for item in errors)

        after = ai_service.get_project_analysis_config(project.id)
        assert after["weekly_interval_minutes"] == 15, "失败的那次不该动已存的配置"
        assert after["max_analysis_rounds"] == 8, "仍是默认值：既没被夹取也没被写入"


def test_the_endpoint_fields_round_trip():
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ok, _message, errors = ai_service.update_project_analysis_config(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1/",
                "api_model": "deepseek-v4-flash",
            },
        )
        assert ok is True
        assert errors == []

        config = ai_service.get_project_analysis_config(project.id)
        assert config["api_base_url"] == "http://127.0.0.1:15721/v1", "地址要归一化"
        assert config["api_model"] == "deepseek-v4-flash"
        assert config["source"] == "custom", "非官方地址应判成自定义端点"


def test_build_endpoint_client_prefers_the_typed_values():
    """「未保存也能测」：请求体里填了就用填的，留空才回退到已保存的值。"""
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        client, errors = ai_service.build_endpoint_client(project.id, {})
        assert client is None
        assert {item["field"] for item in errors} == {"api_base_url", "api_model", "api_key"}

        ai_service.set_project_api_key(project.id, "k")
        client, errors = ai_service.build_endpoint_client(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1",
                "api_model": "typed-model",
                "api_key": "typed-key",
            },
        )
        assert errors == []
        assert client is not None
        assert client.model == "typed-model"


def test_build_endpoint_client_falls_back_to_the_saved_key():
    """输入框留空表示「沿用已保存的 Token」，而不是「清空」。"""
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ai_service.set_project_api_key(project.id, "saved-key")
        ai_service.update_project_analysis_config(
            project.id,
            {"api_base_url": "http://127.0.0.1:15721/v1", "api_model": "saved-model"},
        )

        client, errors = ai_service.build_endpoint_client(
            project.id, {"api_model": "typed-model"}
        )
        assert errors == []
        assert client.model == "typed-model"

# ==========================================================================
# run 溯源：这结论是哪套 prompt / skill / 规则 / 模型跑出来的
# ==========================================================================


def test_the_provenance_names_the_prompt_skill_rules_and_model():
    """四个溯源值都要有内容，且模型取自项目当前配置。"""
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()
        ai_service.update_project_analysis_config(project.id, {"api_model": "m-1"})
        db.session.commit()

        fingerprint = provenance.current_provenance(project.id)

        assert fingerprint["prompt_version"].startswith("prompt-")
        assert fingerprint["rules_version"], "规则版本为空，改了规则也不会让缓存失效"
        assert fingerprint["skill_version"], "skill 版本为空，改了 skill 也不会让缓存失效"
        assert fingerprint["model"] == "m-1"


def test_a_weekly_run_records_its_provenance():
    """**写入路径**：跑一次分析，run 上要留下「谁跑出来的」。

    这些列在数据模型里加好了，但此前**没有任何写入路径** —— 不记录就没法说明一份结论
    是怎么来的，也没法判断「改了 skill 之后这份结论还算不算数」。

    溯源是在**建 run 的时候**写下的（早于任何模型调用），所以这里不需要真跑通一次分析。

    顺带钉住新行为：**没配好接口就不再假装成功。** 以前这条用例能拿到 `succeeded`，
    因为执行器是假的 —— 它按变更条数算个风险等级就返回了，密钥只当布尔阀门用。
    现在会如实失败，并给出「缺什么」。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("code"), "git", "code")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(cfg, repo, "src/absorber.lua", datetime.now(timezone.utc))
        db.session.commit()
        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(project.id, {"api_model": "m-w"})
        db.session.commit()

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "failed", "接口没配全却报了成功"
        assert "接口地址" in outcome["error_message"], "失败原因没说清缺什么"

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert run.prompt_version and run.rules_version and run.skill_version
        assert run.model == "m-w"


def test_a_failed_run_is_marked_failed_with_a_readable_reason():
    """**`error_message` 这一列此前从来没被写入过。**

    后果是失败的分析在界面上永远是「分析中」，用户看不出它已经失败、更不知道该怎么办。
    现在失败要落 `status="failed"` 加一句能对上号的原因。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("code"), "git", "code")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(cfg, repo, "src/absorber.lua", datetime.now(timezone.utc))
        db.session.commit()
        # Token 有、但接口地址与模型没填：这是「配了一半」的状态，必须建出 run 并如实
        # 记失败。连 Token 都没有的情况会更早返回 skipped（那时还没有 run 可记）。
        ai_service.set_project_api_key(project.id, "k")
        db.session.commit()

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert run.status == "failed"
        assert run.error_message, "失败了却没写原因"
        assert run.finished_at is not None, "失败的 run 也要收尾，否则界面上永远转着"
        assert run.rounds_used == 0, "一次模型调用都没发生"


def test_a_failed_run_writes_no_conclusion_fields():
    """失败**不许**留下看起来像结论的字段。

    缺陷形态（线上实测）：模型调用超时（Read timed out），但库里那条 run 照样写了
    response_payload / response_text，于是它长得和成功记录一样 —— 内嵌的结论里有
    `risk_level: high`。而那个 high 根本不是模型给的，是 `_determine_risk_level`
    按变更规模（total_files >= 120）估出来的兜底值，模型压根没答上来。
    前端只判「有没有结果」，就把面板从「待分析」显示成「已有结果 · 风险等级 high」。
    失败只该留错误，「有没有成功结论」在数据层必须无歧义。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("code"), "git", "code")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(cfg, repo, "src/absorber.lua", datetime.now(timezone.utc))
        db.session.commit()
        ai_service.set_project_api_key(project.id, "k")
        db.session.commit()

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert run.status == "failed"
        assert run.response_payload is None, (
            "失败的 run 写了 response_payload —— 前端会把它当结论渲染，"
            "连按变更规模估出来的兜底风险等级一起显示成「已有结果」"
        )
        assert not (run.response_text or "").strip(), (
            "失败的 run 写了 response_text —— 前端只判这个字段就能显示「已有结果」"
        )
        assert run.error_message, "失败原因仍然要留着，否则用户不知道该改什么"


def test_a_failed_run_is_never_reused_as_a_cached_result():
    """失败的 run 不能当缓存命中，也不能被当成「最近结果」返回。

    缺陷形态：`_is_run_fresh` 只判「有没有内容 / 是不是过期 / 溯源对不对」，不判
    status。失败记录有 response_text（错误文本）、有 finished_at、溯源也齐，
    于是被判为可用 → `stream_*` 直接**回放**这条失败：用户再点一次分析，拿到的是
    上次的失败，而不是重新跑。

    这条用例刻意构造一条**除 status 外完全可复用**的 run：内容非空、时间在窗口内、
    溯源与当前配置逐字一致（`**provenance.current_provenance(...)`）。否则它会因为
    「溯源对不上」而被拒，测试就变成「无论有没有 status 判断都通过」—— 什么也没证明。
    （第一版就踩了这个坑：mutation 掉 status 判断后它照样过。）
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()
        ai_service.update_project_analysis_config(project.id, {"api_model": "m-1"})
        db.session.commit()

        failed = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=1,
            status="failed",
            response_text="调用模型失败（LLMTransportError）：Read timed out.",
            finished_at=datetime.now(timezone.utc),
            **provenance.current_provenance(project.id),
        )
        db.session.add(failed)
        db.session.commit()

        # 先确认这条 run 除了 status 之外确实「够格」被复用：把 status 改成成功就该判 True。
        failed.status = "succeeded"
        db.session.commit()
        assert ai_service._is_run_fresh(failed) is True, (
            "构造的 run 本身不可复用 —— 那下面的断言就不是在验 status 了"
        )

        failed.status = "failed"
        db.session.commit()
        assert ai_service._is_run_fresh(failed) is False, (
            "失败的 run 被判成可用 —— 会被 stream_* 当缓存回放，"
            "用户再点分析拿到的还是上次的失败"
        )


def test_a_failed_run_does_not_advance_the_weekly_watermark():
    """失败不能推进增量水位线。

    `last_analyzed_at` 是增量分析的水位线（用它筛 `updated_at > last_analyzed_at`
    的文件）。一次失败的分析若把它推到当前时刻，那批变更就被整体判成「已看过」，
    下一次分析直接返回 no_change 静默跳过 —— 用户再点多少次都跑不动。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("code"), "git", "code")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(cfg, repo, "src/absorber.lua", datetime.now(timezone.utc))
        db.session.commit()
        ai_service.set_project_api_key(project.id, "k")
        db.session.commit()

        outcome = ai_service.run_weekly_analysis_background(cfg.id)
        assert outcome["status"] == "failed", "这条用例的前提是这次分析失败"

        state = AiWeeklyAnalysisState.query.filter_by(project_id=project.id).first()
        assert state is None or state.last_analyzed_at is None, (
            "失败的分析推进了水位线 —— 这批变更会被判成「已看过」，"
            "下次分析静默跳过"
        )


def test_the_analysis_client_uses_the_configured_timeout():
    """正式分析要用配置里的单次请求超时，不能用探测用的 30 秒。

    缺陷形态：探测（测试连接，prompt 是「只回复两个字」）与正式分析共用
    `build_endpoint_client`，而它把 timeout 写死成 `PROBE_TIMEOUT_SECONDS = 30`。
    正式分析是**非流式**请求，requests 的 timeout 对非流式响应等价于「整个响应体要在
    30 秒内到齐」—— 网关得先吃下几百 KB 的 prompt 再生成完整 JSON 报告，必然超时。
    症状就是「测试连接 1.4 秒成功、正式分析永远 Read timed out」。
    """
    from services.ai.endpoint_service import PROBE_TIMEOUT_SECONDS

    captured = {}

    def _fake_build_probe_client(**kwargs):
        captured.update(kwargs)
        return object()

    with app.app_context():
        create_tables()
        project = _create_project()
        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id, {"api_base_url": "https://gw.example.com/v1", "api_model": "m"}
        )
        db.session.commit()

        monkeypatch_target = ai_service.build_probe_client
        ai_service.build_probe_client = _fake_build_probe_client
        try:
            ai_service.build_endpoint_client(project.id, {}, timeout_seconds=300)
            assert captured["timeout_seconds"] == 300, (
                f"分析超时没传下去，拿到的是 {captured['timeout_seconds']}"
            )
            captured.clear()
            # 不传时仍然是探测值 —— 测试连接必须继续用短超时
            ai_service.build_endpoint_client(project.id, {})
            assert captured["timeout_seconds"] == PROBE_TIMEOUT_SECONDS
        finally:
            ai_service.build_probe_client = monkeypatch_target


def test_the_configured_timeout_is_clamped_and_never_falls_back_to_probe():
    """配置里的超时是脏值时按范围夹紧，绝不能退回探测用的 30 秒。

    退回 30 秒 = 分析必然超时，而这正是这次要修的缺陷本身。
    """
    from models.ai_analysis.project_config import (
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        REQUEST_TIMEOUT_RANGE,
    )

    low, high = REQUEST_TIMEOUT_RANGE
    assert ai_service._coerce_timeout(None) == DEFAULT_REQUEST_TIMEOUT_SECONDS
    assert ai_service._coerce_timeout("not-a-number") == DEFAULT_REQUEST_TIMEOUT_SECONDS
    assert ai_service._coerce_timeout(1) == low, "低于下界要夹紧"
    assert ai_service._coerce_timeout(999999) == high, "高于上界要夹紧"
    assert ai_service._coerce_timeout("120") == 120, "表单来的是字符串"
    assert ai_service._coerce_timeout(None) > 30, "回落的默认值必须大于探测值"


def test_a_run_without_provenance_is_never_reused():
    """老库上的行这些列是 NULL，**一律判为不可复用**。

    代价是老提交会被重新分析一次，换来的是「绝不会把旧规则下的结论当成新规则下的
    结论展示给用户」。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        stale = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=1,
            status="succeeded",
            response_text="旧结论",
            finished_at=datetime.now(timezone.utc),
        )
        db.session.add(stale)
        db.session.commit()

        assert ai_service._is_run_fresh(stale) is False


def test_changing_the_skill_prompt_or_model_invalidates_the_cache():
    """**这条是这次要修的核心**：改了 skill / 提示词 / 规则 / 模型，旧结论就不能再当
    「现成的」拿来用。

    以前缓存只按「目标 + 时间」命中，于是改完 skill 之后 90 天内重看老提交，拿到的还是
    旧规则下的结论 —— 从用户角度看就是「我的改动没生效」。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()
        ai_service.update_project_analysis_config(project.id, {"api_model": "m-1"})
        db.session.commit()

        fresh = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=1,
            status="succeeded",
            response_text="结论",
            finished_at=datetime.now(timezone.utc),
            **provenance.current_provenance(project.id),
        )
        db.session.add(fresh)
        db.session.commit()

        assert ai_service._is_run_fresh(fresh) is True, "完全一致的溯源应当可复用"

        for field, value in (
            ("prompt_version", "prompt-outdated"),
            ("skill_version", "skill-outdated"),
            ("rules_version", "rules-outdated"),
            ("analysis_revision", "sev=critical;conf=very_high"),
            ("model", "another-model"),
        ):
            original = getattr(fresh, field)
            setattr(fresh, field, value)
            assert ai_service._is_run_fresh(fresh) is False, f"{field} 变了却仍被当成现成的"
            setattr(fresh, field, original)

        assert ai_service._is_run_fresh(fresh) is True, "改回去之后应当恢复可复用"


def test_changing_the_severity_threshold_invalidates_the_cached_conclusion():
    """**改门槛必须逼出一次重跑。**

    三个版本号都是**源码内容哈希**，而 `rules_version()` 只哈希 `rules.py` 一个文件
    （`RULE_SOURCE_FILES`）—— 用户改 `min_severity` / `min_confidence` /
    `max_anomalies_per_run` 时它逐字不变。而那几项直接决定「哪些结论会被报出来」：
    把门槛从 `high` 收到 `critical` 之后重看老提交，拿到的会是旧门槛下归一化的结论，
    连「规则变了」那句提示都不出现 —— 从用户角度看就是「我的改动没生效」。

    规则层早就写清楚了这件事（`RuleThresholds.revision_component` 的 docstring：
    「门槛变了就是另一个问题，**必须**重跑」），那个方法也一直在，只是**没有任何生产
    调用点** —— `AiAnalysisRun.analysis_revision` 这一列从建出来起就没被写过。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()
        ai_service.update_project_analysis_config(project.id, {"min_severity": "high"})
        db.session.commit()

        run = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=1,
            status="succeeded",
            response_text="结论",
            finished_at=datetime.now(timezone.utc),
            **provenance.current_provenance(project.id),
        )
        db.session.add(run)
        db.session.commit()

        assert run.analysis_revision, "门槛指纹没有被写进溯源列"
        assert ai_service._is_run_fresh(run) is True

        ai_service.update_project_analysis_config(project.id, {"min_severity": "critical"})
        db.session.commit()

        assert ai_service._is_run_fresh(run) is False, (
            "门槛从 high 收到 critical 了，老结论仍被当成现成的 —— "
            "用户会以为新门槛没生效"
        )
        # `rules_version` 是源码哈希，改配置**不该**动它 —— 这正是非要有这一位的理由
        assert provenance.current_provenance(project.id)["rules_version"] == run.rules_version


def test_a_broken_threshold_config_does_not_break_the_freshness_check():
    """库里的门槛被手工改坏时：`from_config` 抛错，而读侧不能因此 500。

    给一个独有的取值即可 —— 非法配置与任何历史结论都不相等，照样逼出一次重跑，
    而重跑时会以正常路径把配置错误报出来。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()
        from models.ai_analysis import AiProjectAnalysisConfig

        ai_service.update_project_analysis_config(project.id, {"min_severity": "high"})
        db.session.commit()
        config = AiProjectAnalysisConfig.query.filter_by(project_id=project.id).first()
        config.min_severity = "不是个门槛"
        db.session.commit()

        fingerprint = provenance.current_provenance(project.id)

        assert fingerprint["analysis_revision"] == "invalid-config"
        run = AiAnalysisRun(
            project_id=project.id, target_type="commit", target_id=1, status="succeeded",
            response_text="结论", finished_at=datetime.now(timezone.utc), **fingerprint,
        )
        db.session.add(run)
        db.session.commit()
        assert ai_service._is_run_fresh(run) is True, "自己写进去的溯源应当与现算的一致"

        config.min_severity = "high"
        db.session.commit()
        assert ai_service._is_run_fresh(run) is False, "修好配置之后应当重跑一次"


def test_the_freshness_check_still_honours_the_time_window():
    """时间窗不能被溯源判等挤掉：太老的结论即使版本一致也不复用。"""
    with app.app_context():
        create_tables()
        project = _create_project()
        db.session.commit()

        ancient = AiAnalysisRun(
            project_id=project.id,
            target_type="commit",
            target_id=1,
            status="succeeded",
            response_text="结论",
            finished_at=datetime.now(timezone.utc)
            - timedelta(days=ai_service.ANALYSIS_CACHE_DAYS + 1),
            **provenance.current_provenance(project.id),
        )
        db.session.add(ancient)
        db.session.commit()

        assert ai_service._is_run_fresh(ancient) is False


# ==========================================================================
# 端到端：模型真的被调用了，结论真的落库了
# ==========================================================================


COMMIT_SHA = "a" * 40
TABLE_PATH = "config/[30]道具表_CfgItem.xlsx"


class _FakeClient:
    """只回答一次 final。记录它收到的消息，用来断言上下文真的组装过。

    `report_usage=False` 用来模拟**上游压根不回 `usage`** 的网关（真实存在：某些中转
    网关、某些被限流时的响应）。这时 token 必须落成 NULL，不是 0。
    """

    def __init__(self, *, report_usage: bool = True):
        self.calls = []
        self.report_usage = report_usage

    def complete(self, messages, *, temperature=None):
        from services.ai.llm_client import ChatResult

        self.calls.append([dict(item) for item in messages])
        import json as _json

        return ChatResult(
            text=_json.dumps(
                {
                    "status": "final",
                    "report_markdown": "# 变更理解\n\n道具表删了一行。\n\n# 风险评估\n\n中高。\n",
                    "anomalies": [
                        {
                            "title": "【道具】删除了已放出的 ID",
                            "category": "config_id",
                            "severity": "critical",
                            "confidence": "high",
                            "evidence": [f"{TABLE_PATH} 删除了 ID 1001"],
                            "commit": COMMIT_SHA,
                            "file_path": TABLE_PATH,
                            "impact": "老存档引用的道具会失效",
                            "suggestion": "确认是否有意下线",
                        }
                    ],
                    "dimensions": [{"id": "config_id", "hit": True, "note": "有删除"}],
                },
                ensure_ascii=False,
            ),
            model="fake",
            prompt_tokens=120 if self.report_usage else None,
            completion_tokens=80 if self.report_usage else None,
        )


def test_a_real_run_calls_the_model_and_persists_the_findings(monkeypatch):
    """**这条是「线接上了」的证据。**

    在它之前，平台的执行器是假的：按变更条数算个风险等级就返回，密钥只当布尔阀门用，
    `ai_analysis_anomaly` 与 `ai_analysis_trace` 两张表从来没有被写入过。

    这里用一个假 client 替掉真实 HTTP，断言完整的链路：配置 → 变更集 → 提示词 →
    引擎 → 接地校验 → 门槛过滤 → run / trace / anomaly 三张表。

    **子代理模式显式关掉**：它现在默认开，而这条用例量的是**单代理那条链**（一次调用、
    一条 trace）。开着它跑的是「3 个分片 + 1 次汇总」，那是
    `tests/test_ai_subagent_wiring.py` 的事。
    """
    from models.ai_analysis import AiAnalysisAnomaly, AiAnalysisTrace

    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("table"), "svn", "table")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(
            cfg, repo, TABLE_PATH, datetime.now(timezone.utc), commit_id=COMMIT_SHA
        )
        db.session.commit()

        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1",
                "api_model": "deepseek-v4-flash",
                "subagent_enabled": False,
            },
        )
        db.session.commit()

        client = _FakeClient()
        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (client, []))

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        assert len(client.calls) == 1, "模型没有被调用"

        # 提示词真的组装过：变更清单里有那个文件，且明确说了「diff 不在这里」。
        user = client.calls[0][-1]["content"]
        assert TABLE_PATH in user
        assert "没有任何 diff" in user

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert run.status == "succeeded"
        assert "道具表删了一行" in run.response_text
        assert run.rounds_used == 1
        assert run.tokens_input == 120 and run.tokens_output == 80

        rows = AiAnalysisAnomaly.query.filter_by(run_id=run.id).all()
        assert [row.title for row in rows] == ["【道具】删除了已放出的 ID"]
        assert rows[0].severity == "critical"
        assert rows[0].fingerprint, "没有指纹，下一轮就没法判重"
        assert AiAnalysisTrace.query.filter_by(run_id=run.id).count() == 1

        # --- 用量也接上了（同一次真实运行）-------------------------------------
        # 采集点在 `_persist_outcome` 这一个漏斗里，所以三个入口（SSE 流式 / 后台任务 /
        # 定时）都会走到这里。这里钉的是「真的写进去了」，而不只是「字段存在」。
        assert run.tokens_input == 120 and run.tokens_output == 80
        assert run.rounds_used == 1
        assert run.tool_requests_used is not None
        assert run.context_chars is not None, "没有记账，面板上的「上下文塞了多少」永远是空"
        assert run.duration_ms is not None, "没记耗时"
        assert run.anomalies_found == 1
        assert run.dropped_count is not None
        # 这个假 client 不回缓存字段 → 必须是 NULL，**不是 0**（口径见 services/ai/usage.py）
        assert run.cache_read_tokens is None
        assert run.cache_source is None

        trace = AiAnalysisTrace.query.filter_by(run_id=run.id).one()
        assert trace.tokens_input == 120 and trace.tokens_output == 80
        assert trace.request_chars, "逐轮的提示词字符数没写"

        # 抽屉那一行读的就是它（`_result_payload` 里的 usage 子字典）
        assert run.response_payload is not None
        payload = json.loads(run.response_payload)
        assert payload["usage"]["tokens"]["total"] == 200
        assert payload["usage"]["collected"] is True
        assert payload["usage"]["cache"]["hit_rate"] is None, (
            "上游没报缓存却给出了命中率 —— 界面会显示一个用户会当真的 0%"
        )

        # 面板的读取路径：这一行在「AI 消耗」页面上的样子
        from services.ai_usage_service import run_usage

        detail = run_usage(run.id)
        assert detail["run"]["usage"]["tokens"]["total"] == 200
        assert detail["run"]["usage"]["cache"]["hit_rate"] is None
        # 没配价格表 → 不给金额（**不是 0**）
        assert detail["run"]["usage"]["cost"]["amount"] is None


def test_a_gateway_that_does_not_report_usage_persists_unknown_not_zero(monkeypatch):
    """**上游不回 `usage` 时，库里必须是 NULL。**

    这条以前写成 0，一路上有三个后果，每一个都发生在**用户看得见的地方**：

    * 消耗面板把这次运行算进「输入 0 tokens」的总和里，账面比实际花的少；
    * `pricing.estimate_cost` 只为 `None` 留了「无法估算」这条路，拿到 0 就算出一个
      确定的 `¥0.00` 摆在界面上；
    * `analysis_budget` 那句「有 N 处 token 数上游未上报，已用量是下界」永远不会出现 ——
      也就是**没有人会被告知**这个数字是缺失的。

    平台的钱照付，而账面把「没花钱」摆给用户看。落库这一层是这条链的源头，所以在这里钉。

    与它成对的是 `_FakeClient(report_usage=True)` 那条：每一轮都报的时候，落库的仍是那个
    真实的数（口径没被改成「一律 NULL」）。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("table"), "svn", "table")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(
            cfg, repo, TABLE_PATH, datetime.now(timezone.utc), commit_id=COMMIT_SHA
        )
        db.session.commit()

        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1",
                "api_model": "m",
                "subagent_enabled": False,
            },
        )
        db.session.commit()

        client = _FakeClient(report_usage=False)
        monkeypatch.setattr(ai_service, "build_endpoint_client", lambda *a, **k: (client, []))

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        assert outcome["status"] == "succeeded", outcome
        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert run.tokens_input is None, "上游没报 usage，库里却有个数（0 就是把「不知道」说成「没花」）"
        assert run.tokens_output is None

        from models.ai_analysis import AiAnalysisTrace

        trace = AiAnalysisTrace.query.filter_by(run_id=run.id).one()
        assert trace.tokens_input is None, "逐轮那一份也要如实：这一轮没有用量可记"
        assert trace.tokens_output is None

        # 抽屉与面板读的那两份都不许把「未上报」渲染成 0。
        from services.ai_usage_service import run_usage

        payload = json.loads(run.response_payload)
        assert payload["usage"]["tokens"]["input"] is None
        assert payload["usage"]["tokens"]["total"] is None, (
            "两个分量都不知道，总数却给了个 0 —— 它看起来完全正常"
        )
        detail = run_usage(run.id)
        assert detail["run"]["usage"]["tokens"]["total"] is None
        # 费用那一档：算不出就不能给数字（**尤其不能给 ¥0.00**）
        assert detail["run"]["usage"]["cost"]["amount"] is None


def test_a_finding_below_the_configured_bar_is_not_persisted(monkeypatch):
    """门槛是**落库前**的过滤：没达标的条目不该进 `ai_analysis_anomaly`。

    进不了库才是真正的「不给人工跟进」—— 只在界面上不显示是不够的。
    """
    from models.ai_analysis import AiAnalysisAnomaly

    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("table"), "svn", "table")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(
            cfg, repo, TABLE_PATH, datetime.now(timezone.utc), commit_id=COMMIT_SHA
        )
        db.session.commit()

        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id,
            {
                "api_base_url": "http://127.0.0.1:15721/v1",
                "api_model": "m",
                "min_confidence": "very_high",
            },
        )
        db.session.commit()

        monkeypatch.setattr(
            ai_service, "build_endpoint_client", lambda *a, **k: (_FakeClient(), [])
        )

        outcome = ai_service.run_weekly_analysis_background(cfg.id)

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert AiAnalysisAnomaly.query.filter_by(run_id=run.id).count() == 0
        assert run.status == "succeeded", "门槛没过不等于这次分析失败"


def test_the_second_run_carries_the_first_runs_findings_as_a_baseline(monkeypatch):
    """**这就是「1 小时前分析过、现在只多了 1 个 commit」要说的事。**

    第二次分析不能从零开始：它必须带上「这个版本截至目前已经报过什么」，否则模型会把
    上一轮报过的问题再报一遍，而 QA 每轮的分诊成果都被作废一次 —— 这正是增量评审用不
    下去的根本原因。

    同时钉住「累积一份」：基线取的是**上一次成功运行的那批结论**，不是历次运行的并集。
    并起来会把已经修好的旧条目重新翻出来。
    """
    from tests.test_ai_analysis_service import COMMIT_SHA, TABLE_PATH, _FakeClient

    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("table"), "svn", "table")
        cfg = _create_weekly_config(
            project.id, repo, "W1", datetime(2026, 3, 1), datetime(2026, 3, 8)
        )
        _seed_diff_cache(cfg, repo, TABLE_PATH, datetime.now(timezone.utc), commit_id=COMMIT_SHA)
        db.session.commit()

        ai_service.set_project_api_key(project.id, "k")
        ai_service.update_project_analysis_config(
            project.id,
            # 同上：显式关掉子代理模式，这条用例量的是「上一次的结论有没有进这一次的
            # 提示词」，而分片之后的提示词是各分片自己那一份（基线在别处）。
            {"api_base_url": "http://127.0.0.1:15721/v1", "api_model": "m",
             "subagent_enabled": False},
        )
        db.session.commit()

        first_client = _FakeClient()
        monkeypatch.setattr(
            ai_service, "build_endpoint_client", lambda *a, **k: (first_client, [])
        )
        first = ai_service.run_weekly_analysis_background(cfg.id)
        assert first["status"] == "succeeded", first

        # 第一次分析里不该有「历史结论」——那时确实是第一次。
        assert "已经报过的问题" not in first_client.calls[0][-1]["content"]

        # 又来了一个提交：把那条缓存记录推到「上次分析之后」。
        entry = WeeklyVersionDiffCache.query.filter_by(config_id=cfg.id).first()
        entry.updated_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        db.session.commit()

        second_client = _FakeClient()
        monkeypatch.setattr(
            ai_service, "build_endpoint_client", lambda *a, **k: (second_client, [])
        )
        second = ai_service.run_weekly_analysis_background(cfg.id)
        assert second["status"] == "succeeded", second

        user = second_client.calls[0][-1]["content"]
        assert "已经报过的问题" in user, "第二次分析没有带上历史结论"
        assert "【道具】删除了已放出的 ID" in user, "上一轮报过的那条没进基线"
        assert "不要当作新发现重复报" in user
        # 判重靠指纹，指纹得跟着进提示词，模型才能逐条对照。
        assert "#" in user


# ==========================================================================
# 增量水位线：只有「真正跑完」的 run 才能推进
# ==========================================================================


def test_a_degraded_run_does_not_advance_the_weekly_watermark():
    """`degraded`（降级但有报告）不许推进水位线。

    `_persist_outcome` 把 degraded 也存成 `run.status == "succeeded"`，所以旧的
    `if run.status != "succeeded": return` 拦不住它 —— 而 degraded 恰恰是最不该
    推进的那一类：线上那个周版本 767 个文件里有 748 个 `.lua` 的 diff 根本没读到，
    照样被判成「已分析」，增量从此只看得到水位线之后的新文件，那批变更再也不会被
    重新分析。判据必须是引擎侧的 `outcome.status`。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        # 不需要建仓库/周版本配置：`_update_weekly_state` 只看 payload、run、state。
        start = datetime(2026, 3, 1, 0, 0)
        end = datetime(2026, 3, 8, 0, 0)

        run = AiAnalysisRun(
            project_id=project.id,
            target_type="weekly",
            target_key="W1",
            status="succeeded",      # degraded 落库后的样子
            scope="full",
            trigger_source="manual",
        )
        db.session.add(run)
        db.session.commit()

        state = AiWeeklyAnalysisState(
            project_id=project.id,
            group_key="W1",
            base_name="W1",
            start_time=start,
            end_time=end,
            last_analyzed_at=None,
        )
        db.session.add(state)
        db.session.commit()

        payload = {"group": {"project_id": project.id, "key": "W1",
                             "base_name": "W1",
                             "start_time": start.isoformat(), "end_time": end.isoformat()},
                   "summary": {"total_files": 3}}

        # degraded：水位线必须原地不动，下次才会重跑同一批变更
        ai_service._update_weekly_state(payload, run, state, engine_status="degraded")
        assert state.last_analyzed_at is None, "降级的分析推进了水位线，那批变更再也不会被重跑"
        assert state.last_analysis_run_id is None

        # failed：同样不许推进
        ai_service._update_weekly_state(payload, run, state, engine_status="failed")
        assert state.last_analyzed_at is None

        # succeeded：正常推进
        ai_service._update_weekly_state(payload, run, state, engine_status="succeeded")
        assert state.last_analyzed_at is not None, "跑完了却没推进水位线，增量会重复分析"
        assert state.last_analysis_run_id == run.id


def test_the_watermark_judgement_cannot_silently_fall_back_to_run_status():
    """`engine_status` 是必填关键字参数。

    给它默认值（或让它可选）就等于留了一条「忘了传就退回 run.status」的路，
    而 run.status 分不出 degraded —— 那正是这个缺陷本身。
    """
    import inspect

    signature = inspect.signature(ai_service._update_weekly_state)
    param = signature.parameters.get("engine_status")
    assert param is not None, "engine_status 参数没了"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "engine_status 应当是仅关键字参数"
    assert param.default is inspect.Parameter.empty, (
        "engine_status 有默认值 —— 忘了传就会退回分不出 degraded 的 run.status"
    )


def test_concurrent_first_runs_do_not_collide_on_the_state_row(monkeypatch):
    """并发首跑撞唯一约束时，输的一方拿到赢家那一行，而不是把整个 tick 放弃。

    `group_key` 上有唯一约束，而状态行在分析**开跑前**读、写入在分析**结束**时，
    中间隔着整轮 LLM 调用（几十秒）—— 两个标签页同时手动分析同一个分组，或者
    调度器那一 tick 又轮到它，两个调用方都会查到 `None` 并各自 `add`。

    **这条用「让 commit 抛一次 IntegrityError」来复刻那一刻**：真实的并发顺序是
    「A 查 → B 查 → A 插 → B 插炸」，从 B 的角度看就是「查的时候没有、提交的时候
    已经有了」，与这里造的一模一样。不用真起线程：那会引入 `threading` 的时序抖动，
    而这里要卡住的判据只有一条 —— 撞约束后**回滚重查**，而不是把异常咽掉或抛出去。
    """
    from sqlalchemy.exc import IntegrityError

    from services.ai.weekly_state import get_or_create_weekly_state

    with app.app_context():
        create_tables()
        project = _create_project()
        # 必须真提交：下面那一支会 rollback 掉本 session 的待写内容，
        # 只 flush 的话 project 行会被一起撤回，赢家那次插入撞的是外键。
        db.session.commit()
        group_key = _uid("W-concurrent")

        real_commit = db.session.commit
        calls = {"n": 0}

        def commit_that_loses_the_race():
            calls["n"] += 1
            if calls["n"] > 1:
                return real_commit()
            # 赢家（另一个线程 / 另一个请求）在这一刻把同一行提交了进去。
            # 用独立连接写，模拟「不是本 session 干的」。
            db.session.rollback()
            with db.engine.begin() as conn:
                conn.execute(
                    AiWeeklyAnalysisState.__table__.insert().values(
                        project_id=project.id,
                        group_key=group_key,
                        base_name="W-race-winner",
                    )
                )
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

        monkeypatch.setattr(db.session, "commit", commit_that_loses_the_race)

        state = get_or_create_weekly_state(
            project_id=project.id, group_key=group_key, base_name="W-race-loser"
        )

        assert calls["n"] == 1, "没走到「撞约束」那一步，这条用例没验到重查分支"
        assert state is not None, "撞约束后没重查到赢家那一行 —— 异常被咽掉了"
        assert state.base_name == "W-race-winner", "拿回来的不是赢家那一行"
        assert AiWeeklyAnalysisState.query.filter_by(group_key=group_key).count() == 1, (
            "重查分支没生效，库里落了两行同名分组"
        )


def test_a_non_concurrency_integrity_error_is_not_swallowed(monkeypatch):
    """回滚后重查仍然没有那一行 —— 说明不是并发首跑，必须照旧抛出去。

    把 `except IntegrityError` 写成「一律吞掉、返回 None」的话，调用方拿到 `None`
    会在下一行解引用炸掉，报出来的是一个与真实原因（外键 / NOT NULL 约束）毫无
    关系的 `AttributeError`。
    """
    from sqlalchemy.exc import IntegrityError

    from services.ai.weekly_state import get_or_create_weekly_state

    with app.app_context():
        create_tables()
        project = _create_project()

        def commit_that_fails_for_another_reason():
            db.session.rollback()
            raise IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))

        monkeypatch.setattr(db.session, "commit", commit_that_fails_for_another_reason)

        try:
            get_or_create_weekly_state(
                project_id=project.id,
                group_key=_uid("W-broken"),
                base_name="W-broken",
            )
        except IntegrityError:
            pass
        else:
            raise AssertionError("非并发的完整性错误被咽掉了，调用方会拿到 None")


def test_a_small_repository_still_reaches_the_payload_when_truncated(monkeypatch):
    """端到端：取样必须真的作用在 payload 构建上。

    **这条是接线守卫。** 直接测 `_sample_with_repo_fairness` 只能证明那个函数对，
    证明不了它被用上了 —— 把调用点换回 `_limit_items`（原来的直接截断），
    只测函数的用例照样全绿。线上那个「配表被整个挤出清单」的缺陷，
    只有从 `build_weekly_payload` 一路看到清单才拦得住。

    两个仓库的 `type` 都设成 `git`，复刻线上「配表仓库也是 git」这个前提 ——
    优先级退化的根因就在那里。

    **清单的截断现在是按字符算的**（`MAX_LIST_CHARS`），所以这里把它压小来逼出
    退化那一支；`max_files_per_run` 退居「退化时取多少个」。同时断言**白名单不受影响**：
    `delta_files` 仍然是全部 33 个 —— 清单可以少列，白名单不能少给，否则模型连
    `file_diff` 都会被拒（这正是这次要修的信息缺口）。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo_code = _create_repo(project.id, _uid("code"), "git", "code")
        repo_table = _create_repo(project.id, _uid("table"), "git", "table")

        start = datetime(2026, 3, 1, 0, 0)
        end = datetime(2026, 3, 8, 0, 0)
        cfg_code = _create_weekly_config(project.id, repo_code, "W1", start, end)
        cfg_table = _create_weekly_config(project.id, repo_table, "W1", start, end)

        base = datetime.now(timezone.utc) - timedelta(hours=2)
        for i in range(30):
            _seed_diff_cache(cfg_code, repo_code, f"src/file_{i}.lua", base)
        for i in range(3):
            _seed_diff_cache(cfg_table, repo_table, f"config/table_{i}.xlsx", base)
        db.session.commit()

        monkeypatch.setattr(
            ai_service, "get_project_analysis_config",
            lambda *a, **k: {"max_files_per_run": 10},
        )
        monkeypatch.setattr(ai_service, "MAX_LIST_CHARS", 100)
        payload, _state, skip_reason = ai_service.build_weekly_payload(cfg_code.id)
        assert skip_reason is None

        listed = [item["file_path"] for item in payload["list_files"]]
        assert len(listed) == 10, f"没有按上限截断：{len(listed)}"
        table_paths = [path for path in listed if path.startswith("config/")]
        assert len(table_paths) == 3, (
            f"配表仓库被挤出了清单（进了 {len(table_paths)}/3 个）：{listed}"
        )
        assert payload["delta_truncated"] is True
        assert payload["summary"]["total_files"] == 33

        # 白名单必须是全部 33 个 —— 清单少列不等于少给可读范围。
        whitelist = {item["file_path"] for item in payload["delta_files"]}
        assert len(whitelist) == 33, (
            f"白名单被清单的截断连累了（只有 {len(whitelist)}/33 个可读）：{sorted(whitelist)}"
        )
        assert set(listed) <= whitelist
        assert "src/file_29.lua" in whitelist, "没列出来的文件也必须在白名单里"


def test_the_whole_list_is_rendered_when_it_fits(monkeypatch):
    """清单装得下时**不截断**：全列是默认行为，取样只是兜底。

    这条守的是 `MAX_LIST_CHARS` 那个判断本身。没有它，「把取样换成无条件截断」这种
    回退不会被任何用例发现 —— 而那个回退正好会重新制造信息缺口。
    """
    with app.app_context():
        create_tables()
        project = _create_project()
        repo = _create_repo(project.id, _uid("code"), "git", "code")
        cfg = _create_weekly_config(project.id, repo, "W1",
                                    datetime(2026, 3, 1), datetime(2026, 3, 8))
        base = datetime.now(timezone.utc) - timedelta(hours=2)
        for i in range(30):
            _seed_diff_cache(cfg, repo, f"src/file_{i}.lua", base)
        db.session.commit()

        # max_files_per_run 故意设得很小：它不该再决定「列多少」
        monkeypatch.setattr(
            ai_service, "get_project_analysis_config",
            lambda *a, **k: {"max_files_per_run": 5},
        )
        payload, _state, _skip = ai_service.build_weekly_payload(cfg.id)

        assert len(payload["list_files"]) == 30, (
            "清单被 max_files_per_run 截断了 —— 全列才是默认行为"
        )
        assert payload["delta_truncated"] is False
