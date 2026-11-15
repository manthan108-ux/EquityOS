import pytest

from app.research import keys_cli


def test_adds_a_random_admin_key_without_printing_it(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("A=1\nRESEARCH_API_KEYS=alice:s1\nB=2\n", encoding="utf-8")
    assert keys_cli.main(["add", "ops", "--admin", "--env", str(env)]) == 0
    out = capsys.readouterr().out
    text = env.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("RESEARCH_API_KEYS="))
    entries = line.split("=", 1)[1].split(",")
    assert (
        entries[0] == "alice:s1" and entries[1].startswith("ops:") and entries[1].endswith(":admin")
    )
    secret = entries[1].split(":")[1]
    assert len(secret) >= 40 and secret not in out  # not echoed
    assert "A=1" in text and "B=2" in text  # the rest of the file is untouched


def test_show_prints_the_key_once_and_a_second_key_differs(tmp_path, capsys):
    env = tmp_path / ".env"
    keys_cli.main(["add", "a", "--show", "--env", str(env)])
    shown = capsys.readouterr().out.splitlines()[-1]
    assert shown in env.read_text(encoding="utf-8")
    assert keys_cli.add_client(env, "b", admin=False) != shown


def test_rejects_duplicates_and_bad_names(tmp_path):
    env = tmp_path / ".env"
    keys_cli.add_client(env, "a", admin=False)
    with pytest.raises(ValueError):
        keys_cli.add_client(env, "a", admin=False)
    with pytest.raises(ValueError):
        keys_cli.add_client(env, "bad name,x", admin=False)
