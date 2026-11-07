"""Import the pinned team's read-only UI. Never install/publish a collector.

Run from any directory with --source pointing to the independent clone.
Only the UI is copied. Upstream and adapted hashes make changes reviewable.
"""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

REVISION = "dd02ed055606c0be3e756de7a61ffb80e6e913a6"
FILES = (
    "index.html",
    "app.js",
    "data.js",
    "live.js",
    "map-controls.js",
    "sequence.js",
    "diagnostics.js",
    "i18n.js",
    "locales.js",
    "architecture.js",
    "style.css",
    "architecture.css",
    "workspace.css",
    "sequence.css",
    "diagnostics.css",
    "locale.css",
    "architecture-original.jpg",
    "migration-original.jpg",
    "vendor/panzoom-4.6.2.es.js",
    "vendor/README.md",
    "vendor/PANZOOM-LICENSE.txt",
)


def adapt(name, raw):
    if name.endswith(".jpg"):
        return raw
    text = raw.decode()
    if name in {"index.html", "app.js", "data.js", "architecture.js"}:
        text = text.replace("BRAVO /", "SOVA /").replace(
            "Bravo Network Atlas", "Sova Network Atlas"
        )
        text = text.replace("Bravo / Network Atlas", "Sova / Network Atlas")
    if name == "index.html":
        text = text.replace('<span class="brand-mark">b', '<span class="brand-mark">s')
        text = text.replace("<span>BRAVO<span", "<span>SOVA<span")
        text = text.replace("Bravo의 활성", "Sova에서 관측하는 활성")
        text = text.replace(
            '<a href="/team.html">팀 개발 · 서비스 주소 <span>↗</span></a>',
            '<a id="sova-console-back" href="/console/">Sova 운영 콘솔로 돌아가기 ↗</a>',
        )
        text = text.replace(
            '<main id="main-content">',
            '<main id="main-content"><p id="sova-network-scope" role="status">공유 환경의 읽기 전용 관측 · 앱의 실제 의존성이나 이전 성공 판정이 아닙니다.</p>',
        )
        text = text.replace(
            '<script type="module" src="app.js">',
            '<script type="module" src="bridge.js"></script><script type="module" src="app.js">',
        )
    if name == "style.css":
        text = re.sub(r"@import url\([^)]*\);", "", text)
        text += "\n#sova-network-scope{padding:12px 18px;background:#eef3ff;border:1px solid #dce5ff;border-radius:8px;color:#334e83;font-size:13px;line-height:1.6}#sova-network-scope:empty{display:none}\n"
    if name == "architecture.js":
        text = text.replace("file:'/", "file:'")
    if name == "i18n.js":
        # No geo-location or edge trace request is made by the embedded product view.
        start = text.index(" let country=null;", text.index("async function initialize()"))
        end = text.index(" automatic=chooseLocale", start)
        text = text[:start] + " const country=null;\n" + text[end:]
        text = text.replace("bravo-atlas-locale", "sova-network-locale")
    if name == "live.js":
        text = (
            "import {networkURL, clearNetworkScope, showNetworkScope} from './bridge.js';\n" + text
        )
        text = text.replace("function accept(d){", "function accept(d){showNetworkScope(d);")
        text = text.replace(
            "const socketLabels={",
            "const transportLabel=typeof location!=='undefined'&&location.protocol==='https:'?'WSS':'WS';\nconst socketLabels={",
        )
        for label in ("연결 중", "연결됨", "재연결 중", "수신 오류"):
            text = text.replace("'WSS " + label + "'", "`${transportLabel} " + label + "`")
        text = text.replace(
            "`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/stream`",
            "networkURL('stream',true)",
        )
        text = text.replace("fetch('/api/status',", "fetch(networkURL('status'),")
        text = text.replace(
            "if(!r.ok)throw Error('HTTP');",
            "if(!r.ok){data=null;clearNetworkScope([401,403].includes(r.status)?undefined:'관측 권한·등록·수집 상태를 확인할 수 없어 자료를 지웠습니다.');render();throw Error('HTTP')}",
        )
        text = text.replace(
            "ws.onclose=()=>{",
            "ws.onclose=event=>{if([4401,4403].includes(event.code)){data=null;browserError=true;socketState='error';clearNetworkScope();render();return}",
        )
        text = text.replace(
            "else if(m.type==='error'){browserError=true;",
            "else if(m.type==='error'){data=null;clearNetworkScope('관측 권한·등록·수집 상태를 확인할 수 없어 자료를 지웠습니다.');browserError=true;",
        )
    return text.encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    expected = subprocess.check_output(
        ["git", "rev-parse", REVISION], cwd=source, text=True
    ).strip()
    if revision != expected or subprocess.check_output(
        ["git", "status", "--porcelain", "--", "web"], cwd=source
    ):
        raise SystemExit("Expected the pinned, unmodified upstream UI")
    destination = Path(__file__).resolve().parents[1] / "src/bravo_console/static/network"
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {
        "repository": "https://github.com/Softbank-Hackathon-Finals/b-cluster",
        "revision": revision,
        "files": {},
    }
    for name in FILES:
        raw = (source / "web" / name).read_bytes()
        adapted = adapt(name, raw)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(adapted)
        manifest["files"][name] = {
            "upstream_sha256": hashlib.sha256(raw).hexdigest(),
            "adapted_sha256": hashlib.sha256(adapted).hexdigest(),
        }
    (destination / "upstream.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"Imported {len(FILES)} UI assets from {revision}; no collector, DNS or publishing code imported."
    )


if __name__ == "__main__":
    main()
