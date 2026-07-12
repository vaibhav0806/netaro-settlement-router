#!/usr/bin/env python3
"""Run Docker end-to-end scenarios in isolated Compose projects."""

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    project_name: str
    api_host_port: int
    postgres_host_port: int
    artifact_dir: Path
    payout_mode: str


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    payout_mode: str
    pytest_marker: str | None
    needs_internal_audit: bool = False
    is_load: bool = False


SCENARIOS = {
    spec.name: spec
    for spec in (
        ScenarioSpec("boot-contract", "load", "boot_contract"),
        ScenarioSpec("settlement-idempotency", "load", "settlement_idempotency"),
        ScenarioSpec("provider-reconciliation", "random", "provider_reconciliation"),
        ScenarioSpec(
            "concurrency-funds",
            "load",
            "concurrency_funds",
            needs_internal_audit=True,
        ),
        ScenarioSpec(
            "lifecycle-recovery",
            "random",
            "lifecycle_recovery",
            needs_internal_audit=True,
        ),
    )
}

ARTIFACT_NAMES = (
    "metadata.txt",
    "compose-config.yml",
    "compose-ps.txt",
    "images.txt",
    "readiness.log",
    "scenario.stdout",
    "scenario.stderr",
    "api.log",
    "db.log",
    "internal-audit.txt",
    "teardown.txt",
)
PORT_COLLISION_PATTERNS = (
    "port is already allocated",
    "bind for",
    "failed to bind",
)


def compose_command(config: RunConfig, *args: str) -> list[str]:
    return ["docker", "compose", "-p", config.project_name, *args]


def _run(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, capture_output=True, text=True)


def _environment(config: RunConfig) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "API_HOST_PORT": str(config.api_host_port),
            "POSTGRES_HOST_PORT": str(config.postgres_host_port),
            "PAYOUT_MODE": config.payout_mode,
        }
    )
    return environment


def _candidate_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _fresh_ports() -> tuple[int, int]:
    api_port = _candidate_port()
    postgres_port = _candidate_port()
    while postgres_port == api_port:
        postgres_port = _candidate_port()
    return api_port, postgres_port


def _new_config(spec: ScenarioSpec) -> RunConfig:
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    api_port, postgres_port = _fresh_ports()
    return RunConfig(
        run_id=run_id,
        project_name=f"netaro-e2e-{run_id.lower()}",
        api_host_port=api_port,
        postgres_host_port=postgres_port,
        artifact_dir=Path(".artifacts/e2e") / run_id,
        payout_mode=spec.payout_mode,
    )


def _initialize_artifacts(config: RunConfig) -> None:
    config.artifact_dir.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACT_NAMES:
        (config.artifact_dir / name).touch()


def _default_health_request(url: str) -> tuple[int, object]:
    try:
        with urlopen(url, timeout=2) as response:
            try:
                body: object = json.loads(response.read())
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = "invalid JSON"
            return response.status, body
    except HTTPError as error:
        try:
            body: object = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = "invalid JSON"
        return error.code, body
    except (OSError, URLError) as error:
        return 0, {"error": str(error)}


def wait_for_health(
    config: RunConfig,
    *,
    timeout_seconds: float = 60,
    request: Callable[[str], tuple[int, object]] = _default_health_request,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + timeout_seconds
    health_url = f"http://127.0.0.1:{config.api_host_port}/health"
    readiness_path = config.artifact_dir / "readiness.log"
    readiness_path.parent.mkdir(parents=True, exist_ok=True)

    while True:
        status, body = request(health_url)
        rendered_body = json.dumps(body, sort_keys=True)
        with readiness_path.open("a") as readiness:
            readiness.write(f"{status} {rendered_body}\n")
        if status == 200 and body == {"status": "ok"}:
            return
        if monotonic() >= deadline:
            raise TimeoutError(
                f"health endpoint did not return exact readiness within "
                f"{timeout_seconds} seconds"
            )
        sleep(0.25)


def _redact(text: str) -> str:
    text = re.sub(
        r"(?im)^(\s*[^\n:#]*password[^\n:]*:\s*)([^\n]+)$",
        r"\1REDACTED",
        text,
    )
    text = re.sub(
        r"(?i)(\b[\w-]*password[\w-]*\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1REDACTED",
        text,
    )
    return re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@]+)(@)",
        r"\1REDACTED\3",
        text,
    )


def _persist(path: Path, text: str, *, append: bool = False) -> None:
    mode = "a" if append else "w"
    with path.open(mode) as output:
        output.write(_redact(text))


def _command_output(command: list[str], config: RunConfig) -> str:
    try:
        result = _run(command, env=_environment(config))
    except OSError as error:
        return _redact(f"command failed: {error}\n")
    output = result.stdout
    if result.stderr:
        output += result.stderr
    if result.returncode:
        output += f"exit_code={result.returncode}\n"
    return _redact(output)


def capture_artifacts(
    config: RunConfig,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    _initialize_artifacts(config)
    config_result = _command_output(compose_command(config, "config"), config)
    (config.artifact_dir / "compose-config.yml").write_text(_redact(config_result))

    evidence_commands = {
        "compose-ps.txt": compose_command(config, "ps", "--all"),
        "images.txt": compose_command(config, "images"),
        "api.log": compose_command(config, "logs", "--no-color", "api"),
        "db.log": compose_command(config, "logs", "--no-color", "db"),
    }
    for name, command in evidence_commands.items():
        (config.artifact_dir / name).write_text(
            _redact(_command_output(command, config))
        )

    git_sha = _command_output(["git", "rev-parse", "HEAD"], config).strip()
    dirty = bool(_command_output(["git", "status", "--porcelain"], config).strip())
    docker_version = _command_output(
        ["docker", "version", "--format", "{{.Server.Version}}"], config
    ).strip()
    compose_version = _command_output(
        compose_command(config, "version", "--short"), config
    ).strip()
    container_ids = _command_output(compose_command(config, "ps", "-q"), config).strip()
    image_ids = _command_output(compose_command(config, "images", "-q"), config).strip()
    volume_ids = _command_output(
        [
            "docker",
            "volume",
            "ls",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={config.project_name}",
        ],
        config,
    ).strip()
    metadata = "\n".join(
        (
            f"run_id={config.run_id}",
            f"tested_sha={git_sha}",
            f"dirty={str(dirty).lower()}",
            f"utc_started={(started_at or datetime.now(UTC)).isoformat()}",
            f"utc_finished={(finished_at or datetime.now(UTC)).isoformat()}",
            f"host={platform.system()} {platform.release()} "
            f"{platform.machine()} node={platform.node()}",
            f"python={platform.python_version()}",
            f"docker={docker_version}",
            f"compose={compose_version}",
            f"project={config.project_name}",
            f"api_host_port={config.api_host_port}",
            f"postgres_host_port={config.postgres_host_port}",
            f"payout_mode={config.payout_mode}",
            f"container_ids={container_ids}",
            f"image_ids={image_ids}",
            f"volume_ids={volume_ids}",
        )
    )
    (config.artifact_dir / "metadata.txt").write_text(_redact(metadata) + "\n")


def _is_port_collision(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(pattern in lowered for pattern in PORT_COLLISION_PATTERNS)


def _scenario_command(spec: ScenarioSpec) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/e2e/test_docker_api.py",
        "-q",
    ]
    if spec.pytest_marker:
        command.extend(["-m", spec.pytest_marker])
    return command


def _internal_audit(config: RunConfig) -> subprocess.CompletedProcess[str]:
    sql = """
BEGIN TRANSACTION READ ONLY;
SELECT 'negative_accounts=' || count(*) FROM accounts WHERE balance < 0;
SELECT 'unbalanced_groups=' || count(*) FROM (
  SELECT journal_id, currency FROM postings GROUP BY journal_id, currency
  HAVING coalesce(sum(amount) FILTER (WHERE side = 'DEBIT'), 0)
      <> coalesce(sum(amount) FILTER (WHERE side = 'CREDIT'), 0)
) invalid;
SELECT 'duplicate_owner_keys=' || count(*) FROM (
  SELECT owner_id, idempotency_key FROM settlements
  GROUP BY owner_id, idempotency_key HAVING count(*) > 1
) duplicate_rows;
SELECT 'duplicate_events=' || count(*) FROM (
  SELECT settlement_id, event FROM journal_transactions
  WHERE settlement_id IS NOT NULL GROUP BY settlement_id, event
  HAVING count(*) > 1
) duplicate_rows;
SELECT 'unposted_journals=' || count(*)
FROM journal_transactions WHERE is_posted = false;
COMMIT;
"""
    return _run(
        compose_command(
            config,
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-U",
            "netaro",
            "-d",
            "netaro",
            "-c",
            sql,
        ),
        env=_environment(config),
    )


def _verify_lifecycle(config: RunConfig) -> subprocess.CompletedProcess[str]:
    settlement_ids = (
        (config.artifact_dir / "lifecycle-settlement-id.txt").read_text().splitlines()
    )
    formerly_pending_id = settlement_ids[1]
    outputs: list[str] = []
    restart = _run(compose_command(config, "restart", "api"), env=_environment(config))
    outputs.append(restart.stdout + restart.stderr)
    if restart.returncode:
        return subprocess.CompletedProcess(
            [], restart.returncode, "".join(outputs), restart.stderr
        )
    try:
        wait_for_health(config)
    except TimeoutError as error:
        return subprocess.CompletedProcess([], 1, "".join(outputs), str(error))

    for settlement_id in settlement_ids:
        status, body = _default_health_request(
            f"http://127.0.0.1:{config.api_host_port}/settlements/{settlement_id}"
        )
        outputs.append(f"post_restart_get={status} {body}\n")
        if status != 200:
            return subprocess.CompletedProcess(
                [], 1, "".join(outputs), "persistence check failed"
            )
        if settlement_id == formerly_pending_id and (
            not isinstance(body, dict)
            or body.get("status") not in {"SUCCESS", "FAILED"}
        ):
            return subprocess.CompletedProcess(
                [], 1, "".join(outputs), "automatic recovery did not finalize"
            )

    stop = _run(compose_command(config, "stop", "db"), env=_environment(config))
    outputs.append(stop.stdout + stop.stderr)
    if stop.returncode:
        return subprocess.CompletedProcess(
            [], stop.returncode, "".join(outputs), stop.stderr
        )

    deadline = time.monotonic() + 10
    health_status = 0
    health_body: object = {}
    while time.monotonic() < deadline:
        health_status, health_body = _default_health_request(
            f"http://127.0.0.1:{config.api_host_port}/health"
        )
        if health_status == 503:
            break
        time.sleep(0.25)
    outputs.append(f"database_down_health={health_status} {health_body}\n")
    if health_status != 503:
        return subprocess.CompletedProcess(
            [], 1, "".join(outputs), "expected health 503"
        )

    start = _run(compose_command(config, "start", "db"), env=_environment(config))
    outputs.append(start.stdout + start.stderr)
    if start.returncode:
        return subprocess.CompletedProcess(
            [], start.returncode, "".join(outputs), start.stderr
        )
    try:
        wait_for_health(config)
    except TimeoutError as error:
        return subprocess.CompletedProcess([], 1, "".join(outputs), str(error))
    outputs.append("database_recovered_health=200\n")
    return subprocess.CompletedProcess([], 0, "".join(outputs), "")


def run_scenario(spec: ScenarioSpec, *, config: RunConfig | None = None) -> int:
    active_config = config or _new_config(spec)
    started_at = datetime.now(UTC)
    outcome = 1

    try:
        try:
            _initialize_artifacts(active_config)
            startup_succeeded = False
            for attempt in range(3):
                up = _run(
                    compose_command(active_config, "up", "-d", "--build"),
                    env=_environment(active_config),
                )
                _persist(
                    active_config.artifact_dir / "readiness.log",
                    up.stdout + up.stderr,
                    append=True,
                )
                if up.returncode == 0:
                    startup_succeeded = True
                    break
                if not _is_port_collision(up.stderr) or attempt == 2:
                    _persist(active_config.artifact_dir / "scenario.stdout", up.stdout)
                    _persist(active_config.artifact_dir / "scenario.stderr", up.stderr)
                    outcome = up.returncode or 1
                    break
                cleanup = _run(
                    compose_command(active_config, "down", "-v", "--remove-orphans"),
                    env=_environment(active_config),
                )
                _persist(
                    active_config.artifact_dir / "teardown.txt",
                    cleanup.stdout + cleanup.stderr,
                    append=True,
                )
                api_port, postgres_port = _fresh_ports()
                active_config = replace(
                    active_config,
                    api_host_port=api_port,
                    postgres_host_port=postgres_port,
                )

            if startup_succeeded:
                wait_for_health(active_config)
                scenario_environment = _environment(active_config)
                scenario_environment.update(
                    {
                        "E2E_BASE_URL": (
                            f"http://127.0.0.1:{active_config.api_host_port}"
                        ),
                        "E2E_SCENARIO": spec.name,
                        "E2E_ARTIFACT_DIR": str(active_config.artifact_dir.resolve()),
                    }
                )
                scenario = _run(_scenario_command(spec), env=scenario_environment)
                if scenario.returncode == 0 and spec.name == "lifecycle-recovery":
                    lifecycle = _verify_lifecycle(active_config)
                    scenario = subprocess.CompletedProcess(
                        scenario.args,
                        lifecycle.returncode,
                        scenario.stdout + lifecycle.stdout,
                        scenario.stderr + lifecycle.stderr,
                    )
                if scenario.returncode == 0 and spec.needs_internal_audit:
                    audit = _internal_audit(active_config)
                    audit_text = audit.stdout + audit.stderr
                    _persist(
                        active_config.artifact_dir / "internal-audit.txt",
                        audit_text,
                    )
                    expected = {
                        "negative_accounts=0",
                        "unbalanced_groups=0",
                        "duplicate_owner_keys=0",
                        "duplicate_events=0",
                        "unposted_journals=0",
                    }
                    observed = set(audit.stdout.splitlines())
                    if audit.returncode or not expected <= observed:
                        scenario = subprocess.CompletedProcess(
                            scenario.args,
                            audit.returncode or 1,
                            scenario.stdout,
                            scenario.stderr + audit_text,
                        )
                _persist(
                    active_config.artifact_dir / "scenario.stdout",
                    scenario.stdout,
                )
                _persist(
                    active_config.artifact_dir / "scenario.stderr",
                    scenario.stderr,
                )
                outcome = scenario.returncode
        except (OSError, TimeoutError) as error:
            try:
                _persist(
                    active_config.artifact_dir / "scenario.stderr",
                    f"{type(error).__name__}: {error}\n",
                    append=True,
                )
            except OSError:
                pass
            outcome = 1
        finally:
            try:
                capture_artifacts(
                    active_config,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            except Exception as error:
                try:
                    _persist(
                        active_config.artifact_dir / "scenario.stderr",
                        f"artifact capture failed: {error}\n",
                        append=True,
                    )
                except OSError:
                    pass
                if outcome == 0:
                    outcome = 1
    finally:
        try:
            teardown = _run(
                compose_command(active_config, "down", "-v", "--remove-orphans"),
                env=_environment(active_config),
            )
            if outcome == 0 and teardown.returncode:
                outcome = teardown.returncode
            try:
                _persist(
                    active_config.artifact_dir / "teardown.txt",
                    teardown.stdout
                    + teardown.stderr
                    + f"exit_code={teardown.returncode}\n",
                    append=True,
                )
            except OSError:
                pass
        except OSError as error:
            if outcome == 0:
                outcome = 1
            try:
                _persist(
                    active_config.artifact_dir / "teardown.txt",
                    f"teardown failed: {error}\n",
                    append=True,
                )
            except OSError:
                pass

    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    arguments = parser.parse_args(argv)
    return run_scenario(SCENARIOS[arguments.scenario])


if __name__ == "__main__":
    raise SystemExit(main())
