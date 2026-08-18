from doubao2_api.credentials import CredentialStore


def test_empty_when_file_missing(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    assert store.is_empty()
    assert store.cookies == {}


def test_save_and_reload(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save({"sessionid": "abc", "uid": "1"})
    assert store.cookies == {"sessionid": "abc", "uid": "1"}

    reloaded = CredentialStore(path)
    assert reloaded.cookies == {"sessionid": "abc", "uid": "1"}
    assert not reloaded.is_empty()


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "credentials.json"
    CredentialStore(path).save({"a": "b"})
    assert path.exists()


def test_device_roundtrip(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save({"sessionid": "abc"}, device={"device_id": "1", "web_id": "2", "tea_uuid": "2"})
    assert CredentialStore(path).device == {"device_id": "1", "web_id": "2", "tea_uuid": "2"}


def test_save_without_device_keeps_existing(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.save({"a": "b"}, device={"device_id": "1"})
    store.save({"a": "c"})
    assert store.device == {"device_id": "1"}
