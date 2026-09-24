"""Settings precedence (argument > environment > config file > default) and
configuration errors caught before any document is read."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from docket import config
from docket.errors import ConfigurationError

# Subprocesses must import this checkout's docket, not whichever is installed.
SRC = str(Path(config.__file__).resolve().parents[1])


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch, tmp_path):
    """configure() rewrites module attributes; put the test process's own back."""
    monkeypatch.chdir(tmp_path)
    saved = {s.name: getattr(config, s.name) for s in config.SETTINGS}
    saved.update(SOURCES=config.SOURCES, ERRORS=config.ERRORS, CONFIG_FILE=config.CONFIG_FILE)
    yield
    for name, value in saved.items():
        setattr(config, name, value)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file_or_environment():
    loaded = config.load(environ={})
    assert loaded.errors == [] and loaded.file is None
    assert loaded.values["OCR_BACKEND"] == "auto"
    assert loaded.values["OCR_FALLBACKS"] == ["vlm"]
    assert loaded.values["BATCH_WORKERS"] == 4
    assert loaded.values["REVIEW_QUEUE_ENABLED"] is False
    assert set(loaded.sources.values()) == {"default"}


def test_file_overrides_default_and_environment_overrides_file(tmp_path):
    file = write(tmp_path / "docket.toml", """
[ocr]
backend = "Paddle"
fallbacks = ["tesseract", "vlm"]
languages = ["en", "de"]
dpi = 300

[batch]
workers = 8

[paddle]
tables = true
""")
    loaded = config.load(file, environ={"DOCKET_BATCH_WORKERS": "2", "DOCKET_OCR_FALLBACKS": "vlm"})
    assert loaded.errors == []
    values, sources = loaded.values, loaded.sources
    assert values["OCR_BACKEND"] == "paddle" and sources["OCR_BACKEND"] == f"file {file}"
    assert values["OCR_LANGUAGES"] == ["en", "de"] and values["OCR_DPI"] == 300 and values["PADDLE_TABLES"] is True
    assert values["BATCH_WORKERS"] == 2 and sources["BATCH_WORKERS"] == "env DOCKET_BATCH_WORKERS"
    assert values["OCR_FALLBACKS"] == ["vlm"]
    assert sources["TEXT_MODEL"] == "default"


def test_arguments_override_everything(tmp_path):
    from docket import OcrOptions, ProcessOptions
    from docket.options import resolve

    write(tmp_path / "docket.toml", '[ocr]\nlanguages = ["fr"]\nfallbacks = []\n[layout]\ninclude = false\n')
    config.configure(environ={"DOCKET_OCR_LANGUAGES": "de"}, discover=True)
    assert config.OCR_LANGUAGES == ["de"]  # env beats the file
    assert resolve(ProcessOptions()).acquisition.settings.languages == ["de"]
    assert resolve(ProcessOptions()).include_layout is False  # from the file
    explicit = resolve(ProcessOptions(ocr=OcrOptions(languages=["it"]), include_layout=True))
    assert explicit.acquisition.settings.languages == ["it"] and explicit.include_layout is True


def test_config_file_discovery(tmp_path):
    write(tmp_path / "docket.toml", "[batch]\nworkers = 3\n")
    assert config.load(environ={}).values["BATCH_WORKERS"] == 4  # a library ignores ./docket.toml
    assert config.load(environ={}, discover=True).values["BATCH_WORKERS"] == 3
    other = write(tmp_path / "other.toml", "[batch]\nworkers = 5\n")
    assert config.load(environ={"DOCKET_CONFIG": str(other)}).values["BATCH_WORKERS"] == 5
    assert config.load(tmp_path / "docket.toml", environ={"DOCKET_CONFIG": str(other)}).values["BATCH_WORKERS"] == 3


def test_relative_paths_are_relative_to_the_file(tmp_path):
    folder = tmp_path / "conf"
    folder.mkdir()
    file = write(folder / "docket.toml", '[einvoice]\ndownloads = "downloads"\n[api]\njobs_dir = "/srv/jobs"\n')
    values = config.load(file, environ={"DOCKET_REVIEW_DOCUMENTS": "docs"}).values
    assert values["EINVOICE_DOWNLOADS"] == folder / "downloads"
    assert values["JOBS_DIR"] == Path("/srv/jobs")
    assert values["REVIEW_DOCUMENTS_DIR"] == Path("docs")  # env: relative to the working directory


@pytest.mark.parametrize(
    "environ, fragment",
    [
        ({"DOCKET_BATCH_WORKERS": "many"}, "DOCKET_BATCH_WORKERS='many': expected an integer"),
        ({"DOCKET_BATCH_WORKERS": "0"}, "must be at least 1"),
        ({"DOCKET_OCR_MIN_CONFIDENCE": "1.5"}, "must be at most 1"),
        ({"DOCKET_PADDLE_TABLES": "maybe"}, "expected true or false"),
        ({"DOCKET_LLM_PROVIDER": "anthropic"}, "expected one of ollama, openai"),
        ({"DOCKET_PADDLE_MODEL": "large"}, "expected one of mobile, medium"),
        ({"DOCKET_TESSERACT_PSM": "14"}, "must be at most 13"),
    ],
)
def test_invalid_environment_values(environ, fragment):
    loaded = config.load(environ=environ)
    assert len(loaded.errors) == 1 and fragment in loaded.errors[0]


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("[ocr]\ndpi = '300'\n", "[ocr] dpi = '300': expected an integer"),
        ("[ocr]\ndpi = 3.5\n", "expected an integer"),
        ("[ocr]\nfallbacks = [1, 2]\n", "expected a list of strings"),
        ("[ocr]\nbakend = 'tesseract'\n", "unknown setting [ocr] bakend"),
        ("[orc]\nbackend = 'tesseract'\n", "unknown setting [orc] backend"),
        ("workers = 3\n", "'workers' must be a [section] table"),
        ("[batch\nworkers = 3\n", "docket.toml:"),
        ("[layout]\ninclude = 'yes'\n", "expected true or false"),
    ],
)
def test_invalid_config_file(tmp_path, text, fragment):
    loaded = config.load(write(tmp_path / "docket.toml", text), environ={})
    assert len(loaded.errors) == 1 and fragment in loaded.errors[0]


def test_missing_named_config_file(tmp_path):
    loaded = config.load(environ={"DOCKET_CONFIG": str(tmp_path / "nope.toml")})
    assert loaded.errors == [f"config file {tmp_path / 'nope.toml'} (from DOCKET_CONFIG) does not exist"]


def test_invalid_values_keep_defaults_and_check_lists_every_problem(tmp_path):
    write(tmp_path / "docket.toml", "[ocr]\ndpi = 'high'\nlanguages = ['en', 'xx']\n[paddle]\ndevice = 'tpu'\n")
    config.configure(environ={"DOCKET_BATCH_WORKERS": "-1"}, discover=True)  # never raises
    assert config.OCR_DPI == 200 and config.BATCH_WORKERS == 4
    with pytest.raises(ConfigurationError) as exc:
        config.check()
    message = str(exc.value)
    for fragment in ("dpi = 'high'", "DOCKET_BATCH_WORKERS='-1'", "xx", "paddle.device", "'tpu'"):
        assert fragment in message


def test_describe_masks_secrets_and_names_sources(tmp_path):
    write(tmp_path / "docket.toml", '[api]\nkey = "s3cret"\n')
    config.configure(environ={"DOCKET_OCR_BACKEND": "tesseract"}, discover=True)
    rows = {row["key"]: row for row in config.describe()}
    assert rows["api.key"]["value"] == "****" and rows["api.key"]["source"].startswith("file ")
    assert rows["ocr.backend"] == {**rows["ocr.backend"], "value": "tesseract", "source": "env DOCKET_OCR_BACKEND"}
    assert rows["llm.api_key"]["value"] is None
    assert "s3cret" not in json.dumps(config.describe())


def test_every_setting_is_documented():
    keys = [s.key for s in config.SETTINGS]
    envs = [s.env for s in config.SETTINGS]
    assert len(set(keys)) == len(keys) and len(set(envs)) == len(envs)
    assert all(s.help for s in config.SETTINGS)
    example = (Path(__file__).parent.parent / "docket.example.toml").read_text(encoding="utf-8")
    loaded = config.load(Path(__file__).parent.parent / "docket.example.toml", environ={})
    assert loaded.errors == []
    for setting in config.SETTINGS:
        assert setting.key.split(".")[1] in example, setting.key


# ---- entry points refuse to start --------------------------------------------------------


def run_cli(*argv: str) -> int:
    from docket import cli

    try:
        cli.main(list(argv))
    except SystemExit as exc:
        return exc.code
    return 0


def test_cli_config_errors_stop_before_processing(tmp_path, monkeypatch, capsys):
    from docket import cli

    monkeypatch.setattr(cli, "process_document", lambda *a: pytest.fail("processed despite bad config"))
    bad = write(tmp_path / "bad.toml", "[batch]\nworkers = 0\n")
    document = write(tmp_path / "doc.txt", "hello")
    assert run_cli("--config", str(bad), "process", str(document)) == 3
    assert "workers = 0" in capsys.readouterr().err
    assert run_cli("--config", str(tmp_path / "missing.toml"), "schemas", "list") == 3


def test_cli_config_show_and_check(tmp_path, capsys):
    good = write(tmp_path / "good.toml", '[ocr]\nbackend = "tesseract"\n')
    assert run_cli("--config", str(good), "config", "show", "--format", "json") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["config_file"] == str(good) and shown["errors"] == []
    backend = next(r for r in shown["settings"] if r["key"] == "ocr.backend")
    assert backend["value"] == "tesseract" and backend["source"] == f"file {good}"
    assert run_cli("--config", str(good), "config", "check") == 0
    assert "ok" in capsys.readouterr().out

    bad = write(tmp_path / "bad.toml", "[ocr]\ndpi = 10\n")
    assert run_cli("--config", str(bad), "config", "check") == 3
    assert "must be at least 72" in capsys.readouterr().err
    assert run_cli("--config", str(bad), "config", "show") == 3  # still prints the table
    assert "ocr.dpi" in capsys.readouterr().out


def test_library_refuses_invalid_config_before_reading(tmp_path, monkeypatch):
    from docket import process_document

    monkeypatch.setattr(config, "ERRORS", ["DOCKET_OCR_DPI='x': expected an integer"])
    with pytest.raises(ConfigurationError, match="DOCKET_OCR_DPI"):
        process_document(tmp_path / "never-read.pdf")


def test_http_server_refuses_to_start(monkeypatch):
    from fastapi.testclient import TestClient

    from docket import api

    monkeypatch.setattr(config, "ERRORS", ["DOCKET_MAX_CONCURRENT_JOBS='0': must be at least 1"])
    with pytest.raises(ConfigurationError, match="MAX_CONCURRENT_JOBS"), TestClient(api.app):
        pass


def test_docket_api_command_exits_with_code_3(tmp_path, monkeypatch, capsys):
    import sys

    from docket import api

    bad = write(tmp_path / "bad.toml", "[api]\nmax_concurrent_jobs = 0\n")
    monkeypatch.setattr(sys, "argv", ["docket-api", "--config", str(bad)])
    with pytest.raises(SystemExit) as exc:
        api.run()
    assert exc.value.code == 3
    assert "max_concurrent_jobs = 0" in capsys.readouterr().err


def test_importing_docket_reads_no_files_from_the_working_directory(tmp_path):
    """A library must not pick up whatever .env or docket.toml sits where its
    host runs; only the applications do (configure_app)."""
    import subprocess
    import sys

    write(tmp_path / ".env", "DOCKET_TEXT_MODEL=from-dotenv\n")
    write(tmp_path / "docket.toml", '[batch]\nworkers = 7\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKET_")} | {"PYTHONPATH": SRC}
    code = (
        "import os, docket\n"
        "from docket import config\n"
        "print(config.TEXT_MODEL, config.BATCH_WORKERS, os.environ.get('DOCKET_TEXT_MODEL'))\n"
        "config.configure_app()\n"
        "print(config.TEXT_MODEL, config.BATCH_WORKERS)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=True).stdout.splitlines()
    default_model = next(s.default for s in config.SETTINGS if s.name == "TEXT_MODEL")
    assert out[0] == f"{default_model} 4 None"
    assert out[1] == "from-dotenv 7"


def test_library_warnings_do_not_reach_stderr_unless_the_host_logs(tmp_path):
    import subprocess
    import sys

    code = "import logging, docket; logging.getLogger('docket.x').warning('noisy')"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True,
                            env=os.environ | {"PYTHONPATH": SRC})
    assert "noisy" not in result.stderr
    code = "import logging, docket; logging.basicConfig(); logging.getLogger('docket.x').warning('shown')"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True,
                            env=os.environ | {"PYTHONPATH": SRC})
    assert "shown" in result.stderr


def test_review_storage_without_the_extra_is_a_configuration_error(monkeypatch):
    import importlib.util

    from docket import ProcessOptions, ReviewOptions
    from docket.options import resolve

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "sqlalchemy" else real(name, *a))
    assert resolve(ProcessOptions(review=ReviewOptions(enqueue=False))).review.enqueue is False
    with pytest.raises(ConfigurationError, match=r"docket-idp\[review\]"):
        resolve(ProcessOptions(review=ReviewOptions(enqueue=True)))
