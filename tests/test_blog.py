"""Checks of the Blog: python3 tests/test_blog.py

Discord text becomes HTML without letting a message write any HTML of its
own, and a deleted message takes its post and files with it.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "dota_stats.db")
os.environ["SITE_UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["CONTENT_CHANNEL_ID"] = "42"
os.environ.setdefault("DISCORD_TOKEN", "test")

import content_mirror            # noqa: E402
from web import store            # noqa: E402
from web.discord_md import render  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    out = str(render('<img src=x onerror=alert(1)> <script>x</script>'))
    check("a message's own tags are text", "<img" not in out and "<script" not in out, out)
    out = str(render('[x](javascript:alert(1)) https://e.com/"onmouseover=alert(1)'))
    check("only http(s) links, and a quote ends one", 'href="javascript' not in out
          and 'onmouseover=alert(1)"' not in out and 'href="https://e.com/"' in out, out)
    out = str(render("<@1> said **hi** in <#2>", {"u:1": "<b>Bob</b>", "c:2": "general"}))
    check("mentions by name, escaped", "@&lt;b&gt;Bob&lt;/b&gt;" in out and "#general" in out
          and "<strong>hi</strong>" in out, out)
    out = str(render("see https://docs.google.com/d/a_b_c/edit."))
    check("a link keeps its underscores and drops the full stop",
          'href="https://docs.google.com/d/a_b_c/edit"' in out and "</a>." in out, out)
    out = str(render("```\n<b>**not bold**</b>\n```"))
    check("code is left as written", "&lt;b&gt;**not bold**&lt;/b&gt;" in out, out)

    content_mirror.CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    pic = content_mirror.CONTENT_DIR / "7.png"
    pic.write_bytes(b"x")
    post = {"id": 1, "channel_id": 42, "created_at": "2026-11-07T00:00:00+00:00",
            "attachments": [{"kind": "image", "url": "/uploads/content/7.png"}]}
    check("a new post is stored", store.save_content_post(post))
    check("the same post again changes nothing", not store.save_content_post(post))
    store.save_content_post({**post, "id": 2, "attachments": []})
    content_mirror.remove([1])
    check("a deleted message's post is gone", store.content_post(1) is None and store.content_post(2))
    check("and so is its picture", not pic.exists())
    check("other channels aren't copied", content_mirror.is_content(42) and not content_mirror.is_content(43))

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
