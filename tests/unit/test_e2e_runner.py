import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "e2e" / "run.py"
SPEC = importlib.util.spec_from_file_location("e2e_run", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


def make_config(tmp_path: Path) -> run.RunConfig:
    return run.RunConfig(
        run_id="contract-run",
        project_name="netaro-e2e-contract-run",
        api_host_port=48123,
        postgres_host_port=48124,
        artifact_dir=tmp_path,
        payout_mode="load",
    )


def completed(
    command: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_compose_command_always_uses_the_exact_project(tmp_path):
    config = make_config(tmp_path)

    assert run.compose_command(config, "up", "-d") == [
        "docker",
        "compose",
        "-p",
        "netaro-e2e-contract-run",
        "up",
        "-d",
    ]


def test_run_scenario_scopes_compose_and_passes_ports_and_mode(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_subprocess(command, *, env=None, capture_output, text):
        calls.append((command, env or {}))
        return completed(command)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(run, "wait_for_health", lambda *args, **kwargs: None)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 0
    compose_calls = [item for item in calls if item[0][:2] == ["docker", "compose"]]
    assert compose_calls
    assert all(
        command[:4] == ["docker", "compose", "-p", config.project_name]
        for command, _ in compose_calls
    )
    up_env = next(env for command, env in calls if "up" in command)
    assert up_env["API_HOST_PORT"] == "48123"
    assert up_env["POSTGRES_HOST_PORT"] == "48124"
    assert up_env["PAYOUT_MODE"] == "load"
    scenario_env = next(env for command, env in calls if "pytest" in command)
    assert scenario_env["E2E_BASE_URL"] == "http://127.0.0.1:48123"
    assert scenario_env["E2E_SCENARIO"] == "boot-contract"
    assert scenario_env["E2E_ARTIFACT_DIR"] == str(tmp_path.resolve())


def test_wait_for_health_requires_exact_status_and_body(tmp_path):
    config = make_config(tmp_path)
    responses = iter(
        [
            (503, {"detail": "database unavailable"}),
            (200, {"status": "warming"}),
            (200, {"status": "ok"}),
        ]
    )
    times = iter([0.0, 0.1, 0.2, 0.3])

    run.wait_for_health(
        config,
        timeout_seconds=1,
        request=lambda _: next(responses),
        monotonic=lambda: next(times),
        sleep=lambda _: None,
    )

    readiness = (tmp_path / "readiness.log").read_text()
    assert "503" in readiness
    assert json.dumps({"status": "warming"}, sort_keys=True) in readiness
    assert readiness.rstrip().endswith('200 {"status": "ok"}')


def test_wait_for_health_times_out_without_declaring_success(tmp_path):
    config = make_config(tmp_path)
    times = iter([0.0, 0.4, 1.1])

    with pytest.raises(TimeoutError, match="health endpoint"):
        run.wait_for_health(
            config,
            timeout_seconds=1,
            request=lambda _: (200, {"status": "warming"}),
            monotonic=lambda: next(times),
            sleep=lambda _: None,
        )

    assert '200 {"status": "ok"}' not in (tmp_path / "readiness.log").read_text()


@pytest.mark.parametrize("body", [b"not-json", b"\xff"])
def test_malformed_successful_health_body_times_out_cleanly(
    monkeypatch, tmp_path, body
):
    config = make_config(tmp_path)
    times = iter([0.0, 1.1])

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return body

    monkeypatch.setattr(run, "urlopen", lambda *args, **kwargs: Response())

    with pytest.raises(TimeoutError, match="health endpoint"):
        run.wait_for_health(
            config,
            timeout_seconds=1,
            monotonic=lambda: next(times),
            sleep=lambda _: None,
        )

    assert "invalid JSON" in (tmp_path / "readiness.log").read_text()


@pytest.mark.parametrize("scenario_code", [0, 7])
def test_artifacts_are_captured_before_teardown_on_every_outcome(
    monkeypatch, tmp_path, scenario_code
):
    config = make_config(tmp_path)
    events: list[str] = []

    def fake_subprocess(command, *, env=None, capture_output, text):
        if "pytest" in command:
            events.append("scenario")
            return completed(command, scenario_code, "scenario out", "scenario err")
        if "down" in command:
            events.append("teardown")
        return completed(command)

    original_capture = run.capture_artifacts

    def capture(*args, **kwargs):
        events.append("capture")
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(run, "wait_for_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "capture_artifacts", capture)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == scenario_code
    assert events.index("scenario") < events.index("capture") < events.index("teardown")


def test_scenario_exit_code_survives_teardown_failure(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    commands: list[list[str]] = []

    def fake_subprocess(command, *, env=None, capture_output, text):
        commands.append(command)
        if "pytest" in command:
            return completed(command, 9)
        if "down" in command:
            return completed(command, 12, stderr="teardown failed")
        return completed(command)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(run, "wait_for_health", lambda *args, **kwargs: None)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 9
    assert run.compose_command(config, "down", "-v", "--remove-orphans") in commands
    assert "teardown failed" in (tmp_path / "teardown.txt").read_text()


def test_teardown_runs_when_startup_raises(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    commands: list[list[str]] = []

    def fake_subprocess(command, *, env=None, capture_output, text):
        commands.append(command)
        if "up" in command:
            raise OSError("docker unavailable")
        return completed(command)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 1
    assert commands[-1] == run.compose_command(config, "down", "-v", "--remove-orphans")


def test_teardown_runs_if_capture_and_capture_error_write_both_raise(
    monkeypatch, tmp_path
):
    config = make_config(tmp_path)
    commands: list[list[str]] = []
    capture_failed = False
    original_open = Path.open

    def fake_subprocess(command, *, env=None, capture_output, text):
        commands.append(command)
        return completed(command)

    def fail_capture(*args, **kwargs):
        nonlocal capture_failed
        capture_failed = True
        raise RuntimeError("capture failed")

    def fail_capture_error_write(path, *args, **kwargs):
        if capture_failed and path.name == "scenario.stderr":
            raise OSError("artifact filesystem failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(run, "wait_for_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "capture_artifacts", fail_capture)
    monkeypatch.setattr(Path, "open", fail_capture_error_write)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 1
    assert commands[-1] == run.compose_command(config, "down", "-v", "--remove-orphans")


def test_port_selection_never_returns_the_same_port_twice(monkeypatch):
    candidates = iter([41000, 41000, 41001])
    monkeypatch.setattr(run, "_candidate_port", lambda: next(candidates))

    assert run._fresh_ports() == (41000, 41001)


def test_up_retries_with_fresh_ports_only_for_proven_bind_collision(
    monkeypatch, tmp_path
):
    config = make_config(tmp_path)
    up_environments: list[dict[str, str]] = []
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_subprocess(command, *, env=None, capture_output, text):
        calls.append((command, env or {}))
        if "up" in command:
            up_environments.append(env)
            if len(up_environments) == 1:
                return completed(
                    command, 1, stderr="failed to bind host port: address in use"
                )
        return completed(command)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(run, "_fresh_ports", lambda: (49001, 49002))
    monkeypatch.setattr(run, "wait_for_health", lambda *args, **kwargs: None)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 0
    assert len(up_environments) == 2
    assert up_environments[1]["API_HOST_PORT"] == "49001"
    assert up_environments[1]["POSTGRES_HOST_PORT"] == "49002"
    cleanup = run.compose_command(config, "down", "-v", "--remove-orphans")
    assert [command for command, _ in calls if command == cleanup] == [cleanup, cleanup]


def test_up_does_not_retry_an_unrelated_failure(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    up_commands: list[list[str]] = []

    def fake_subprocess(command, *, env=None, capture_output, text):
        if "up" in command:
            up_commands.append(command)
            return completed(
                command,
                4,
                stderr="build helper reports address already in use",
            )
        return completed(command)

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 4
    assert len(up_commands) == 1


def test_all_persisted_process_output_and_errors_are_redacted(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    secret = "never-persist-this"
    password_url = f"postgresql://netaro:{secret}@db:5432/netaro"

    def fake_subprocess(command, *, env=None, capture_output, text):
        if "up" in command:
            return completed(
                command,
                stdout=f"up-stdout {password_url}\n",
                stderr=f"up-stderr\nPOSTGRES_PASSWORD: {secret}\n",
            )
        if "pytest" in command:
            return completed(
                command,
                stdout=f"scenario-stdout {password_url}\n",
                stderr=f"scenario-stderr\nPOSTGRES_PASSWORD: {secret}\n",
            )
        if "down" in command:
            return completed(
                command,
                stdout=f"down-stdout {password_url}\n",
                stderr=f"down-stderr\nPOSTGRES_PASSWORD: {secret}\n",
            )
        return completed(
            command,
            stdout=f"capture-output {password_url}\nPOSTGRES_PASSWORD: {secret}\n",
        )

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(run, "wait_for_health", lambda *args, **kwargs: None)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 0
    artifacts = {
        path.name: path.read_text() for path in tmp_path.iterdir() if path.is_file()
    }
    assert all(secret not in contents for contents in artifacts.values())
    assert "up-stdout" in artifacts["readiness.log"]
    assert "up-stderr" in artifacts["readiness.log"]
    assert "scenario-stdout" in artifacts["scenario.stdout"]
    assert "scenario-stderr" in artifacts["scenario.stderr"]
    assert "capture-output" in artifacts["compose-config.yml"]
    assert "down-stdout" in artifacts["teardown.txt"]
    assert "down-stderr" in artifacts["teardown.txt"]


def test_persisted_lifecycle_error_is_redacted(monkeypatch, tmp_path):
    config = make_config(tmp_path)
    secret = "never-persist-this"

    def fail_health(*args, **kwargs):
        raise TimeoutError(f"postgresql://netaro:{secret}@db:5432/netaro")

    monkeypatch.setattr(
        run.subprocess, "run", lambda command, **kwargs: completed(command)
    )
    monkeypatch.setattr(run, "wait_for_health", fail_health)

    result = run.run_scenario(
        run.ScenarioSpec("boot-contract", "load", "boot_contract"), config=config
    )

    assert result == 1
    assert secret not in (tmp_path / "scenario.stderr").read_text()
    assert "REDACTED" in (tmp_path / "scenario.stderr").read_text()


def test_capture_artifacts_creates_contract_files_and_redacts_compose_config(
    monkeypatch, tmp_path
):
    config = make_config(tmp_path)
    secret = "super-secret-password"

    def fake_subprocess(command, *, env=None, capture_output, text):
        if "config" in command:
            return completed(
                command,
                stdout=(
                    f"POSTGRES_PASSWORD: {secret}\n"
                    f"DATABASE_URL: postgresql://netaro:{secret}@db:5432/netaro\n"
                ),
            )
        return completed(command, stdout="evidence")

    monkeypatch.setattr(run.subprocess, "run", fake_subprocess)

    run.capture_artifacts(config)

    expected = {
        "metadata.txt",
        "compose-config.yml",
        "compose-ps.txt",
        "images.txt",
        "readiness.log",
        "scenario.stdout",
        "scenario.stderr",
        "api.log",
        "db.log",
        "teardown.txt",
    }
    assert expected <= {path.name for path in tmp_path.iterdir()}
    compose_config = (tmp_path / "compose-config.yml").read_text()
    assert secret not in compose_config
    assert "postgresql://netaro:REDACTED@db:5432/netaro" in compose_config
    metadata = (tmp_path / "metadata.txt").read_text()
    assert "project=netaro-e2e-contract-run" in metadata
    assert "api_host_port=48123" in metadata
    assert "postgres_host_port=48124" in metadata
    assert "payout_mode=load" in metadata
