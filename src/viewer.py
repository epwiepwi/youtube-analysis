"""Recommendations + interactive HTML viewer for human-in-the-loop editing.

After the matcher runs, we know the top-3 candidate scenes per cut. This
module extracts thumbnails for every candidate, writes a portable HTML
viewer that loads them side-by-side, and reads back the user's per-cut
choices so CapCut can be regenerated with them.
"""

from __future__ import annotations

import json
import subprocess
from html import escape
from pathlib import Path

from .vision import ClipAnalysis


def _extract_thumbnail_for_scene(source_file: Path, start: float, end: float,
                                  out_path: Path) -> None:
    if out_path.exists():
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t = start + max(0.0, (end - start) / 2)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
         "-i", str(source_file), "-frames:v", "1", "-vf", "scale=360:-1",
         "-q:v", "6", str(out_path)],
        check=True,
    )


def _safe_filename(scene_id: str) -> str:
    return scene_id.replace("#", "_at_").replace("/", "_").replace("\\", "_").replace(" ", "_")


def build_recommendations(
    cut_entries: list[dict],
    ranked_by_cut: dict[int, list[dict]],
    clips_analysis: dict[str, ClipAnalysis],
    output_dir: Path,
) -> list[dict]:
    """Build the recommendations payload and extract candidate thumbnails.

    Returns a list, one entry per cut, each with narration, current pick,
    and up to 3 ranked candidates with thumbnails saved under output/thumbs/.
    """
    thumbs_dir = output_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    recs: list[dict] = []
    for entry in cut_entries:
        idx = entry["index"]
        picks = ranked_by_cut.get(idx, [])
        cands = []
        for rank, p in enumerate(picks[:3]):
            sid = p.get("scene_id")
            if not sid or sid not in clips_analysis:
                continue
            a = clips_analysis[sid]
            thumb_name = f"{_safe_filename(sid)}.jpg"
            thumb_path = thumbs_dir / thumb_name
            try:
                _extract_thumbnail_for_scene(Path(a.source_file), a.start, a.end, thumb_path)
                thumb_rel = f"thumbs/{thumb_name}"
            except Exception as e:
                thumb_rel = ""
                print(f"    thumbnail failed for {sid}: {e}")
            cands.append({
                "rank": rank + 1,
                "scene_id": sid,
                "source_file": Path(a.source_file).name,
                "start": a.start,
                "end": a.end,
                "description": a.description,
                "tags": a.tags,
                "emotion": a.emotion,
                "visual_impact": a.visual_impact,
                "retention": getattr(a, "retention_value", a.visual_impact),
                "score": p.get("score", 0),
                "reason": p.get("reason", ""),
                "thumbnail": thumb_rel,
            })
        recs.append({
            "cut_index": idx,
            "time": entry.get("time", ""),
            "phase": entry.get("phase", ""),
            "owning_sentence_index": entry.get("owning_sentence_index"),
            "owning_sentence_text": entry.get("owning_sentence_text", ""),
            "reference_target_visual": entry.get("reference_target_visual", ""),
            "candidates": cands,
            "selected_scene_id": cands[0]["scene_id"] if cands else None,
        })
    return recs


def save_recommendations(recs: list[dict], output_dir: Path) -> Path:
    path = output_dir / "recommendations.json"
    path.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_selections(output_dir: Path) -> dict[int, str]:
    path = output_dir / "selections.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # Accept both {cut_0: "id"} and [{cut_index: 0, selected: "id"}] shapes.
    result: dict[int, str] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            key = k.replace("cut_", "")
            try:
                result[int(key)] = str(v)
            except (ValueError, TypeError):
                continue
    elif isinstance(data, list):
        for row in data:
            try:
                result[int(row["cut_index"])] = str(row["selected"])
            except (KeyError, ValueError, TypeError):
                continue
    return result


_VIEWER_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>편집 검토 뷰어</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #111; color: #eee;
         margin: 0; padding: 24px; }
  h1 { margin: 0 0 4px; font-size: 20px; }
  .sub { color: #888; margin-bottom: 24px; font-size: 13px; }
  .cut { background: #1c1c1c; border-radius: 12px; padding: 16px; margin-bottom: 18px; }
  .cut header { display: flex; justify-content: space-between; align-items: baseline;
                margin-bottom: 10px; gap: 12px; flex-wrap: wrap; }
  .cut header .left { flex: 1; min-width: 0; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
           background: #2a2a2a; color: #bbb; margin-right: 6px; }
  .sentence { font-size: 14px; color: #ddd; line-height: 1.4; }
  .time { font-size: 11px; color: #666; margin-left: 6px; }
  .ref { font-size: 11px; color: #8aa; margin-top: 4px; font-style: italic; }
  .cards { display: flex; gap: 10px; flex-wrap: wrap; }
  .card { flex: 1 1 250px; min-width: 200px; background: #252525; border-radius: 8px;
          padding: 10px; cursor: pointer; border: 2px solid transparent;
          transition: border-color .15s, transform .1s; }
  .card:hover { border-color: #555; }
  .card.selected { border-color: #4ade80; background: #1e3a26; }
  .card img { width: 100%; border-radius: 4px; display: block; aspect-ratio: 9/16; object-fit: cover;
              background: #333; }
  .rank { font-size: 11px; color: #888; margin-bottom: 4px; }
  .rank.top { color: #4ade80; font-weight: bold; }
  .desc { font-size: 12px; color: #ccc; margin-top: 6px; line-height: 1.35; }
  .meta { font-size: 10px; color: #777; margin-top: 4px; }
  .reason { font-size: 11px; color: #aaa; margin-top: 6px; font-style: italic; }
  .actions { position: sticky; bottom: 12px; background: #111; padding: 12px 0; z-index: 5; }
  button { background: #4ade80; color: #000; border: 0; padding: 12px 24px; border-radius: 8px;
           font-size: 15px; font-weight: bold; cursor: pointer; margin-right: 8px; }
  button:hover { background: #22c55e; }
  .help { font-size: 12px; color: #888; margin-top: 8px; }
  code { background: #222; padding: 2px 6px; border-radius: 3px; color: #eee; }
</style>
</head>
<body>
  <h1>편집 검토</h1>
  <div class="sub">각 컷마다 추천 후보 3개가 있어요. 원하는 걸 클릭해서 선택한 뒤, 아래 "selections.json 저장" 누르고, <code>-UseSelections</code> 옵션 붙여서 다시 실행하면 그 선택대로 CapCut 프로젝트 재생성됩니다.</div>

  <div id="root"></div>

  <div class="actions">
    <button id="save">selections.json 다운로드</button>
    <button id="reset" style="background:#333;color:#eee;">현재 선택 초기화 (모두 1순위로)</button>
    <div class="help">파일을 <code>output/selections.json</code>에 저장하고, PowerShell에서 <code>.\\run.ps1 ... -UseSelections</code> 실행.</div>
  </div>

<script>
const DATA = __DATA__;
const selections = {};
DATA.forEach(c => { selections[c.cut_index] = c.selected_scene_id; });

function render() {
  const root = document.getElementById('root');
  root.innerHTML = DATA.map(cut => {
    const cards = cut.candidates.map(cand => {
      const isSel = selections[cut.cut_index] === cand.scene_id;
      const rankLabel = cand.rank === 1 ? 'AI 1순위 ⭐' : `${cand.rank}순위`;
      return `
        <div class="card ${isSel ? 'selected' : ''}" data-cut="${cut.cut_index}" data-scene="${cand.scene_id}">
          <div class="rank ${cand.rank === 1 ? 'top' : ''}">${rankLabel} · score ${cand.score}</div>
          <img src="${cand.thumbnail}" alt="">
          <div class="desc">${escapeHtml(cand.description)}</div>
          <div class="meta">${escapeHtml(cand.source_file)} · ${cand.start.toFixed(1)}-${cand.end.toFixed(1)}s · retention ${cand.retention}/10</div>
          <div class="reason">${escapeHtml(cand.reason)}</div>
        </div>
      `;
    }).join('');
    return `
      <div class="cut">
        <header>
          <div class="left">
            <span class="badge">cut ${cut.cut_index}</span>
            <span class="badge">${cut.phase || ''}</span>
            <span class="time">${cut.time}</span>
            <div class="sentence">${escapeHtml(cut.owning_sentence_text)}</div>
            ${cut.reference_target_visual ? `<div class="ref">🎯 참조: ${escapeHtml(cut.reference_target_visual)}</div>` : ''}
          </div>
        </header>
        <div class="cards">${cards}</div>
      </div>
    `;
  }).join('');

  document.querySelectorAll('.card').forEach(el => {
    el.addEventListener('click', () => {
      selections[el.dataset.cut] = el.dataset.scene;
      render();
    });
  });
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

document.getElementById('save').addEventListener('click', () => {
  const payload = {};
  Object.entries(selections).forEach(([k, v]) => { payload[`cut_${k}`] = v; });
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'selections.json';
  a.click();
});

document.getElementById('reset').addEventListener('click', () => {
  DATA.forEach(c => { selections[c.cut_index] = c.selected_scene_id; });
  render();
});

render();
</script>
</body>
</html>"""


def write_viewer_html(recs: list[dict], output_dir: Path) -> Path:
    data_json = json.dumps(recs, ensure_ascii=False)
    html = _VIEWER_HTML.replace("__DATA__", data_json)
    path = output_dir / "viewer.html"
    path.write_text(html, encoding="utf-8")
    return path
