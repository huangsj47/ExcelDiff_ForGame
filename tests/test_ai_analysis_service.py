import uuid
from datetime import datetime, timedelta, timezone

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


def _seed_diff_cache(config: WeeklyVersionConfig, repo: Repository, path: str, updated_at: datetime):
    cache = WeeklyVersionDiffCache(
        config_id=config.id,
        repository_id=repo.id,
        file_path=path,
        file_type="code",
        latest_commit_id=_uid("c"),
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

        provenance = ai_service._current_provenance(project.id)

        assert provenance["prompt_version"].startswith("prompt-")
        assert provenance["rules_version"], "规则版本为空，改了规则也不会让缓存失效"
        assert provenance["skill_version"], "skill 版本为空，改了 skill 也不会让缓存失效"
        assert provenance["model"] == "m-1"


def test_a_weekly_run_records_its_provenance():
    """**写入路径**：跑完一次分析，run 上要留下「谁跑出来的」。

    这些列在数据模型里加好了，但此前**没有任何写入路径** —— 不记录就没法说明一份结论
    是怎么来的，也没法判断「改了 skill 之后这份结论还算不算数」。
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
        assert outcome["status"] == "succeeded", outcome

        run = db.session.get(AiAnalysisRun, outcome["run_id"])
        assert run.prompt_version and run.rules_version and run.skill_version
        assert run.model == "m-w"


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
            **ai_service._current_provenance(project.id),
        )
        db.session.add(fresh)
        db.session.commit()

        assert ai_service._is_run_fresh(fresh) is True, "完全一致的溯源应当可复用"

        for field, value in (
            ("prompt_version", "prompt-outdated"),
            ("skill_version", "skill-outdated"),
            ("rules_version", "rules-outdated"),
            ("model", "another-model"),
        ):
            original = getattr(fresh, field)
            setattr(fresh, field, value)
            assert ai_service._is_run_fresh(fresh) is False, f"{field} 变了却仍被当成现成的"
            setattr(fresh, field, original)

        assert ai_service._is_run_fresh(fresh) is True, "改回去之后应当恢复可复用"


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
            **ai_service._current_provenance(project.id),
        )
        db.session.add(ancient)
        db.session.commit()

        assert ai_service._is_run_fresh(ancient) is False
