import json
import sys

import pytest

import istiocli

ISTIOD_PODS = {"items": [
    {"metadata": {"name": "istiod-a"}, "status": {"phase": "Running"}},
    {"metadata": {"name": "istiod-b"}, "status": {"phase": "Running"}},
    {"metadata": {"name": "istiod-old"}, "status": {"phase": "Succeeded"}},
]}


@pytest.fixture
def fake_cluster(monkeypatch):
    """Ersetzt istiocli.run: 'get' liefert ISTIOD_PODS, 'exec' die Antwort je istiod-Pod."""
    def install(exec_results: dict[str, tuple[int, str, str]]):
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            if "get" in cmd:
                return 0, json.dumps(ISTIOD_PODS), ""
            pod = cmd[cmd.index("exec") + 3]
            return exec_results[pod]
        monkeypatch.setattr(istiocli, "run", fake_run)
        return calls
    return install


def test_xds_status():
    entry = {"cluster_sent": "a", "cluster_acked": "a", "listener_sent": "b", "listener_acked": "a",
             "route_sent": ""}
    assert istiocli.xds_status(entry, "cluster") == "SYNCED"
    assert istiocli.xds_status(entry, "listener") == "STALE"
    assert istiocli.xds_status(entry, "route") == "NOT SENT"
    assert istiocli.xds_status(entry, "endpoint") == "NOT SENT"


def test_fetch_syncz_merges_all_running_istiods(fake_cluster):
    calls = fake_cluster({
        "istiod-a": (0, json.dumps([{"proxy": "web-1.app"}]), ""),
        "istiod-b": (0, json.dumps([{"proxy": "web-2.app"}]), ""),
    })
    entries, errors = istiocli.fetch_syncz(["kubectl"], "istio-system")
    assert entries == [{"proxy": "web-1.app", "istiod": "istiod-a"}, {"proxy": "web-2.app", "istiod": "istiod-b"}]
    assert errors == []
    assert not any("istiod-old" in c for c in calls)


def test_fetch_syncz_keeps_errors_of_single_istiod(fake_cluster):
    """Frueher gingen Fehler eines Pods verloren, sobald ein anderer Eintraege lieferte."""
    fake_cluster({
        "istiod-a": (0, json.dumps([{"proxy": "web-1.app"}]), ""),
        "istiod-b": (1, "", "error: pods/exec is forbidden"),
    })
    entries, errors = istiocli.fetch_syncz(["kubectl"], "istio-system")
    assert [e["proxy"] for e in entries] == ["web-1.app"]
    assert errors == ["istiod-b: error: pods/exec is forbidden"]


def test_fetch_syncz_invalid_json(fake_cluster):
    fake_cluster({"istiod-a": (0, "kein json", ""), "istiod-b": (0, "null", "")})
    entries, errors = istiocli.fetch_syncz(["kubectl"], "istio-system")
    assert entries == []
    assert len(errors) == 1 and errors[0].startswith("istiod-a: ")


def test_get_json_list_and_single_object(monkeypatch):
    monkeypatch.setattr(istiocli, "run", lambda cmd: (0, '{"items": [1, 2]}', ""))
    assert istiocli.get_json(["oc"], "pods") == ([1, 2], None)
    monkeypatch.setattr(istiocli, "run", lambda cmd: (0, '{"kind": "Namespace"}', ""))
    assert istiocli.get_json(["oc"], "namespace", "x") == ([{"kind": "Namespace"}], None)
    monkeypatch.setattr(istiocli, "run", lambda cmd: (1, "", " Forbidden \n"))
    assert istiocli.get_json(["oc"], "pods") == ([], "Forbidden")


def test_run_missing_binary_and_timeout(monkeypatch):
    assert istiocli.run(["gibt-es-nicht-xyz"])[0] == 127
    monkeypatch.setattr(istiocli, "TIMEOUT", 0.2)
    rc, _, err = istiocli.run([sys.executable, "-c", "import time; time.sleep(5)"])
    assert rc == 124 and "Timeout" in err


def test_detect_cli_prefers_oc(monkeypatch):
    monkeypatch.setattr(istiocli.shutil, "which", lambda b: f"/usr/bin/{b}" if b in ("oc", "kubectl") else None)
    assert istiocli.detect_cli(None) == "/usr/bin/oc"
    assert istiocli.detect_cli("kubectl") == "/usr/bin/kubectl"
    monkeypatch.setattr(istiocli.shutil, "which", lambda b: None)
    assert istiocli.detect_cli(None) == "oc"


def test_inspect_shows_syncz_errors_next_to_entries(inspect_script, capsys):
    entries = [{"proxy": "web-1.app", "istiod": "istiod-a", "cluster_sent": "n", "cluster_acked": "n"},
               {"proxy": "other.elsewhere", "istiod": "istiod-a"}]
    inspect_script.section_proxy_status_syncz((entries, ["istiod-b: forbidden"]), "app")
    out = capsys.readouterr().out
    assert "Fehler: istiod-b: forbidden" in out
    assert "web-1.app" in out and "other.elsewhere" not in out
