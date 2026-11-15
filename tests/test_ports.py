"""The two ports are settings (app/ports.py), so two desks can run on one
machine. Every place that used to say 7797 / 7798 asks there."""
import re
import subprocess
from pathlib import Path

import pytest

from app import ports

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_are_the_ports_the_desk_has_always_used():
    assert ports.check({}) == (7798, 7797)
    assert ports.api_base({}) == "http://127.0.0.1:7798"
    assert ports.dashboard_base({}) == "http://127.0.0.1:7797"


def test_a_second_desk_gets_its_own_pair():
    env = {"RONATHAN_API_PORT": "7818", "RONATHAN_DASHBOARD_PORT": " 7817 "}
    assert ports.check(env) == (7818, 7817)
    assert ports.api_base(env) == "http://127.0.0.1:7818"


def test_the_dashboards_are_reached_on_the_address_they_are_bound_to():
    """This desk's own: RONATHAN_DASHBOARD_BIND may name one address (the
    maintainer's probe and bin/ronathan ask there); unset, `localhost` or a
    wildcard is reached on loopback. ronathan-api is always loopback."""
    for bind in ("", " ", "0.0.0.0", "::", "localhost", "127.0.0.1"):
        assert ports.dashboard_base({"RONATHAN_DASHBOARD_BIND": bind}) == \
            "http://127.0.0.1:7797", bind
    env = {"RONATHAN_DASHBOARD_BIND": "192.168.1.20",
           "RONATHAN_DASHBOARD_PORT": "7817"}
    assert ports.dashboard_base(env) == "http://192.168.1.20:7817"
    assert ports.api_base(env) == "http://127.0.0.1:7798"


@pytest.mark.parametrize("env, words", [
    ({"RONATHAN_API_PORT": "abc"}, "not a port number"),
    ({"RONATHAN_DASHBOARD_PORT": "80"}, "between 1024 and 65535"),
    ({"RONATHAN_API_PORT": "70000"}, "between 1024 and 65535"),
    ({"RONATHAN_API_PORT": "7797"}, "must be different"),       # = dashboards
    ({"RONATHAN_API_PORT": "9000", "RONATHAN_DASHBOARD_PORT": "9000"},
     "must be different"),
])
def test_a_bad_value_is_refused_not_defaulted(env, words):
    """Falling back to the default would collide with the desk already
    running there."""
    with pytest.raises(ports.PortError, match=words):
        ports.check(env)


def test_no_port_number_is_written_into_the_code_that_runs():
    """A literal 7797 / 7798 in a URL or a listen call would ignore the
    setting. (Comments, docstrings and the defaults themselves are fine.)"""
    bad = []
    for f in [*ROOT.glob("app/**/*.py"), ROOT / "bin" / "ronathan"]:
        if f.name == "ports.py":
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(:|port\s*=\s*)778[78]\b", code) and \
                    '"""' not in line and "e.g." not in line and \
                    "port_setting" not in line and \
                    not line.lstrip().startswith(('"', "'", "(", "→")):
                bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    # create_server's own defaults are for tests; the service passes both
    bad = [b for b in bad if "def create_server" not in b
           and "api_base=\"http://127.0.0.1:7798\"," not in b
           and "port=7797, env=None" not in b]
    assert not bad, bad


def test_the_services_and_the_script_read_the_settings():
    api = (ROOT / "app/api/server.py").read_text()
    dash = (ROOT / "app/dashboards.py").read_text()
    assert "port=api_port" in api and "ports.check()" in api
    assert "port=dash_port" in dash and "api_base=api_base" in dash
    assert "ports.dashboard_port(env)" in (
        ROOT / "app/telegram_approvals.py").read_text()
    example = (ROOT / "etc/ronathan.env.example").read_text()
    assert "RONATHAN_API_PORT=" in example and "RONATHAN_DASHBOARD_PORT=" in example


def _script_ports(tmp_path, env_text, extra_env=None):
    """What bin/ronathan works out, from a scratch settings file."""
    script = (ROOT / "bin/ronathan").read_text()
    fn = script[script.index("port_setting() {"):script.index('DASH_URL="')]
    env_file = tmp_path / "ronathan.env"
    env_file.write_text(env_text)
    out = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nENV_FILE="{env_file}"\n{fn}\n'
                       'echo "$API_PORT $DASH_PORT $API_URL"'],
        capture_output=True, text=True, timeout=10,
        env={"PATH": "/usr/bin:/bin", **(extra_env or {})})
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_control_script_reads_the_same_two_settings(tmp_path):
    assert _script_ports(tmp_path, "") == "7798 7797 http://127.0.0.1:7798"
    assert _script_ports(
        tmp_path, "X=1\nRONATHAN_API_PORT=7818\nRONATHAN_DASHBOARD_PORT=7817\n"
    ) == "7818 7817 http://127.0.0.1:7818"
    # empty or not a number: the default, as the services would refuse it
    assert _script_ports(tmp_path, "RONATHAN_API_PORT=\n").startswith("7798 ")
    assert _script_ports(tmp_path, "RONATHAN_API_PORT=abc\n").startswith("7798 ")
    # the environment wins over the file, as it does for the services
    assert _script_ports(tmp_path, "RONATHAN_API_PORT=7818\n",
                         {"RONATHAN_API_PORT": "7808"}).startswith("7808 ")
