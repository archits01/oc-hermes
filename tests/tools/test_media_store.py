import json
from pathlib import Path


def test_put_file_roundtrip_and_dedup(tmp_path, monkeypatch):
    from tools import media_store

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    src = home / "video_gen"
    src.mkdir()
    video = src / "clip.mp4"
    video.write_bytes(b"fake-mp4-bytes")

    first = media_store.put_file(video, kind="video")
    second = media_store.put_file(video)

    assert first["id"] == second["id"]
    assert first["byte_size"] == 14
    assert first["kind"] == "video"
    assert (home / "media.db").is_file()

    loaded = media_store.get_media(first["id"], include_bytes=True)
    assert loaded is not None
    assert loaded["bytes"] == b"fake-mp4-bytes"
    assert loaded["sha256"] == first["sha256"]


def test_ingest_existing_scans_known_dirs(tmp_path, monkeypatch):
    from tools import media_store

    home = tmp_path / ".hermes"
    (home / "video_gen").mkdir(parents=True)
    (home / "images").mkdir(parents=True)
    (home / "pets" / ".thumbs").mkdir(parents=True)
    (home / "video_gen" / "teaser.mp4").write_bytes(b"video-bytes-here")
    (home / "images" / "shot.png").write_bytes(b"png-bytes-here")
    (home / "pets" / ".thumbs" / "skip.png").write_bytes(b"not-ingested")

    monkeypatch.setenv("HERMES_HOME", str(home))
    result = media_store.ingest_existing()

    assert result["scanned"] == 2
    assert result["stored"] == 2
    names = set(result["files"])
    assert names == {"teaser.mp4", "shot.png"}
    assert all(media_store.get_media(mid) for mid in result["ids"])


def test_persist_generated_payload_from_local_file(tmp_path, monkeypatch):
    from tools import media_store

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    image = home / "still.png"
    image.write_bytes(b"png-data")

    payload = {
        "success": True,
        "image": str(image),
        "model": "grok-imagine-image",
        "provider": "xai",
    }
    out = media_store.persist_generated_payload(payload)
    assert out["media_id"]
    assert Path(out["media_db"]).name == "media.db"
    stored = media_store.get_media(out["media_id"], include_bytes=True)
    assert stored["bytes"] == b"png-data"


def test_persist_generated_payload_from_url(tmp_path, monkeypatch):
    from tools import media_store

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    def fake_put_url(url, **kwargs):
        return media_store.put_bytes(
            b"downloaded",
            kind=kwargs.get("kind") or "video",
            filename="remote.mp4",
            source_url=url,
            meta=kwargs.get("meta"),
        )

    monkeypatch.setattr(media_store, "put_url", fake_put_url)
    payload = {
        "success": True,
        "video": "https://files-cdn.x.ai/example.mp4",
        "public_url": "https://files-cdn.x.ai/example.mp4",
    }
    out = media_store.persist_generated_payload(payload)
    assert out["media_filename"] == "remote.mp4"
    assert out["media_bytes"] == 10
