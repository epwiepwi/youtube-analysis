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
    extra_per_cut: int = 7,
) -> tuple[list[dict], list[dict]]:
    """Build per-cut recommendations + a full scene library for browsing.

    Returns (recs, all_scenes).
    - recs: one entry per cut with up to 3 AI picks + `extra_per_cut` more
      alternates from the full pool (pre-filter-scored for this cut).
    - all_scenes: every usable scene with thumbnail + description so the
      viewer can open a full browse panel.
    """
    from .matcher import _score_scene_for_plan  # local import to avoid cycle

    thumbs_dir = output_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    # One-time thumbnail pass for every scene in the pool.
    scene_cards: dict[str, dict] = {}
    print(f"  Extracting {len(clips_analysis)} library thumbnails...")
    for sid, a in clips_analysis.items():
        thumb_name = f"{_safe_filename(sid)}.jpg"
        thumb_path = thumbs_dir / thumb_name
        try:
            _extract_thumbnail_for_scene(Path(a.source_file), a.start, a.end, thumb_path)
            thumb_rel = f"thumbs/{thumb_name}"
        except Exception as e:
            thumb_rel = ""
            print(f"    thumbnail failed for {sid}: {e}")
        scene_cards[sid] = {
            "scene_id": sid,
            "source_file": Path(a.source_file).name,
            "start": a.start,
            "end": a.end,
            "description": a.description,
            "tags": a.tags,
            "emotion": a.emotion,
            "visual_impact": a.visual_impact,
            "retention": getattr(a, "retention_value", a.visual_impact),
            "thumbnail": thumb_rel,
        }

    recs: list[dict] = []
    for entry in cut_entries:
        idx = entry["index"]
        picks = ranked_by_cut.get(idx, [])

        # Build the "AI 추천" set (Gemini's top 3).
        ai_picks: list[dict] = []
        seen_ids: set[str] = set()
        for rank, p in enumerate(picks[:3]):
            sid = p.get("scene_id")
            if not sid or sid not in scene_cards:
                continue
            card = dict(scene_cards[sid])
            card.update({
                "rank": rank + 1,
                "score": p.get("score", 0),
                "reason": p.get("reason", ""),
                "source": "ai",
            })
            ai_picks.append(card)
            seen_ids.add(sid)

        # Heuristically score every remaining scene for this cut using the
        # sentence's plan (reconstructed from the cut entry's fields).
        plan_surrogate = {
            "visual_intent": entry.get("reference_target_visual", "") or entry.get("owning_sentence_text", ""),
            "required_elements": entry.get("reference_target_elements", []),
            "forbidden_elements": [],
            "visual_phase": entry.get("reference_target_phase") or entry.get("phase", ""),
        }
        scored_extras = []
        for sid, a in clips_analysis.items():
            if sid in seen_ids:
                continue
            score = _score_scene_for_plan(a, plan_surrogate)
            scored_extras.append((score, sid))
        scored_extras.sort(reverse=True)

        extras: list[dict] = []
        for score, sid in scored_extras[:extra_per_cut]:
            card = dict(scene_cards[sid])
            card.update({
                "rank": None,
                "score": round(score, 1),
                "reason": "heuristic 대안 (풀 전체에서 추린 추가 후보)",
                "source": "heuristic",
            })
            extras.append(card)

        candidates = ai_picks + extras
        recs.append({
            "cut_index": idx,
            "time": entry.get("time", ""),
            "phase": entry.get("phase", ""),
            "owning_sentence_index": entry.get("owning_sentence_index"),
            "owning_sentence_text": entry.get("owning_sentence_text", ""),
            "reference_target_visual": entry.get("reference_target_visual", ""),
            "candidates": candidates,
            "selected_scene_id": candidates[0]["scene_id"] if candidates else None,
        })

    all_scenes = list(scene_cards.values())
    all_scenes.sort(key=lambda s: (s["source_file"], s["start"]))
    return recs, all_scenes


def save_recommendations(recs: list[dict], all_scenes: list[dict], output_dir: Path) -> Path:
    path = output_dir / "recommendations.json"
    payload = {"recommendations": recs, "all_scenes": all_scenes}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
  .section-label { font-size: 11px; color: #888; margin: 12px 0 6px; text-transform: uppercase;
                   letter-spacing: 0.5px; }
  .cards { display: flex; gap: 10px; overflow-x: auto; padding-bottom: 6px; }
  .card { flex: 0 0 200px; background: #252525; border-radius: 8px;
          padding: 10px; cursor: pointer; border: 2px solid transparent;
          transition: border-color .15s, transform .1s; }
  .card:hover { border-color: #555; }
  .card.selected { border-color: #4ade80; background: #1e3a26; }
  .card.ai .rank { color: #4ade80; font-weight: bold; }
  .card img { width: 100%; border-radius: 4px; display: block; aspect-ratio: 9/16; object-fit: cover;
              background: #333; }
  .rank { font-size: 11px; color: #888; margin-bottom: 4px; }
  .desc { font-size: 12px; color: #ccc; margin-top: 6px; line-height: 1.35; max-height: 4.5em; overflow: hidden; }
  .meta { font-size: 10px; color: #777; margin-top: 4px; }
  .reason { font-size: 11px; color: #aaa; margin-top: 6px; font-style: italic; max-height: 3em; overflow: hidden; }
  .actions { position: sticky; bottom: 12px; background: #111; padding: 12px 0; z-index: 5;
             border-top: 1px solid #222; }
  button { background: #4ade80; color: #000; border: 0; padding: 12px 24px; border-radius: 8px;
           font-size: 15px; font-weight: bold; cursor: pointer; margin-right: 8px; }
  button:hover { background: #22c55e; }
  .browse-btn { background: #3b82f6; color: #fff; padding: 8px 14px; font-size: 12px;
                margin-top: 8px; }
  .browse-btn:hover { background: #2563eb; }
  .help { font-size: 12px; color: #888; margin-top: 8px; }
  code { background: #222; padding: 2px 6px; border-radius: 3px; color: #eee; }
  .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 50;
           display: none; padding: 24px; overflow-y: auto; }
  .modal.open { display: block; }
  .modal-header { display: flex; justify-content: space-between; align-items: center;
                  margin-bottom: 16px; }
  .modal-header h2 { margin: 0; font-size: 18px; }
  .modal-close { background: #333; color: #fff; padding: 6px 12px; font-size: 12px; }
  .modal-filter { padding: 10px; background: #1c1c1c; border-radius: 8px; margin-bottom: 12px;
                  display: flex; gap: 10px; flex-wrap: wrap; }
  .modal-filter input { background: #222; border: 1px solid #333; color: #eee; padding: 8px 12px;
                         border-radius: 6px; flex: 1 1 200px; font-size: 13px; }
  .modal-filter select { background: #222; border: 1px solid #333; color: #eee; padding: 8px 12px;
                          border-radius: 6px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
</style>
</head>
<body>
  <h1>편집 검토</h1>
  <div class="sub">각 컷마다 AI가 3개 추천 + 풀에서 7개 더 추림. "전체 풀에서 고르기"로 모든 장면 볼 수 있음. 썸네일 클릭 → 선택. 하단 "selections.json 저장" 누르고 <code>-UseSelections</code>로 재실행.</div>

  <div id="root"></div>

  <div class="actions">
    <button id="save">selections.json 다운로드</button>
    <button id="reset" style="background:#333;color:#eee;">현재 선택 초기화 (모두 AI 1순위로)</button>
    <div class="help">파일을 <code>output/selections.json</code>에 저장하고 <code>.\\run.ps1 ... -UseSelections</code> 실행.</div>
  </div>

  <div class="modal" id="modal">
    <div class="modal-header">
      <h2 id="modal-title">전체 풀에서 고르기</h2>
      <button class="modal-close" id="modal-close">닫기 ✕</button>
    </div>
    <div class="modal-filter">
      <input type="text" id="filter-text" placeholder="설명 또는 태그 검색 (예: 곰팡이, 누름판, 김치)">
      <select id="filter-file">
        <option value="">전체 파일</option>
      </select>
      <select id="filter-sort">
        <option value="retention">리텐션 높은순</option>
        <option value="impact">임팩트 높은순</option>
        <option value="order">파일 순서</option>
      </select>
    </div>
    <div class="grid" id="modal-grid"></div>
  </div>

<script>
const DATA = __DATA__;
const RECS = DATA.recommendations;
const ALL = DATA.all_scenes;
const selections = {};
RECS.forEach(c => { selections[c.cut_index] = c.selected_scene_id; });

let activeCutForModal = null;

function render() {
  const root = document.getElementById('root');
  root.innerHTML = RECS.map(cut => {
    const ai = cut.candidates.filter(c => c.source === 'ai');
    const extras = cut.candidates.filter(c => c.source !== 'ai');
    const selectedInCurrent = cut.candidates.some(c => c.scene_id === selections[cut.cut_index]);
    const selectedFromPool = !selectedInCurrent && selections[cut.cut_index]
      ? ALL.find(s => s.scene_id === selections[cut.cut_index]) : null;

    const cardHtml = (cand, tag) => {
      const isSel = selections[cut.cut_index] === cand.scene_id;
      const rankText = cand.rank ? `${tag} ${cand.rank}순위 · score ${cand.score}` : `${tag} · score ${cand.score}`;
      return `
        <div class="card ${cand.source === 'ai' ? 'ai' : ''} ${isSel ? 'selected' : ''}"
             data-cut="${cut.cut_index}" data-scene="${cand.scene_id}">
          <div class="rank">${rankText}</div>
          <img src="${cand.thumbnail}" alt="">
          <div class="desc">${escapeHtml(cand.description)}</div>
          <div class="meta">${escapeHtml(cand.source_file)} · ${cand.start.toFixed(1)}-${cand.end.toFixed(1)}s · retention ${cand.retention}/10</div>
          <div class="reason">${escapeHtml(cand.reason || '')}</div>
        </div>
      `;
    };

    const aiHtml = ai.map(c => cardHtml(c, '🤖 AI')).join('');
    const extrasHtml = extras.map(c => cardHtml(c, '대안')).join('');
    const selectedHtml = selectedFromPool ? `
      <div class="section-label">현재 선택 (풀에서 수동 선택)</div>
      <div class="cards">
        <div class="card selected" data-cut="${cut.cut_index}" data-scene="${selectedFromPool.scene_id}">
          <div class="rank" style="color:#4ade80;">직접 선택</div>
          <img src="${selectedFromPool.thumbnail}" alt="">
          <div class="desc">${escapeHtml(selectedFromPool.description)}</div>
          <div class="meta">${escapeHtml(selectedFromPool.source_file)} · ${selectedFromPool.start.toFixed(1)}-${selectedFromPool.end.toFixed(1)}s</div>
        </div>
      </div>` : '';

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
        ${selectedHtml}
        <div class="section-label">🤖 AI 추천 (Top 3)</div>
        <div class="cards">${aiHtml}</div>
        <div class="section-label">📎 유사 대안 (풀에서 자동 추림)</div>
        <div class="cards">${extrasHtml}</div>
        <button class="browse-btn" data-open-pool="${cut.cut_index}">이 컷에 쓸 장면 전체 풀에서 고르기</button>
      </div>
    `;
  }).join('');

  document.querySelectorAll('.card').forEach(el => {
    el.addEventListener('click', () => {
      selections[el.dataset.cut] = el.dataset.scene;
      render();
    });
  });
  document.querySelectorAll('[data-open-pool]').forEach(btn => {
    btn.addEventListener('click', () => openModal(Number(btn.dataset.openPool)));
  });
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}

function openModal(cutIdx) {
  activeCutForModal = cutIdx;
  const cut = RECS.find(c => c.cut_index === cutIdx);
  document.getElementById('modal-title').textContent = `cut ${cutIdx} 장면 고르기 — "${cut.owning_sentence_text}"`;
  populateFilters();
  renderModalGrid();
  document.getElementById('modal').classList.add('open');
}

function populateFilters() {
  const sel = document.getElementById('filter-file');
  const current = sel.value;
  const files = [...new Set(ALL.map(s => s.source_file))].sort();
  sel.innerHTML = '<option value="">전체 파일</option>' +
    files.map(f => `<option value="${escapeHtml(f)}">${escapeHtml(f)}</option>`).join('');
  sel.value = current;
}

function renderModalGrid() {
  const text = document.getElementById('filter-text').value.toLowerCase().trim();
  const file = document.getElementById('filter-file').value;
  const sort = document.getElementById('filter-sort').value;
  let list = ALL.slice();
  if (text) {
    list = list.filter(s => {
      const hay = (s.description + ' ' + (s.tags || []).join(' ')).toLowerCase();
      return hay.includes(text);
    });
  }
  if (file) list = list.filter(s => s.source_file === file);
  if (sort === 'retention') list.sort((a,b) => b.retention - a.retention);
  else if (sort === 'impact') list.sort((a,b) => b.visual_impact - a.visual_impact);
  else list.sort((a,b) => a.source_file.localeCompare(b.source_file) || a.start - b.start);

  const grid = document.getElementById('modal-grid');
  grid.innerHTML = list.map(s => {
    const isSel = selections[activeCutForModal] === s.scene_id;
    return `
      <div class="card ${isSel ? 'selected' : ''}" data-modal-scene="${s.scene_id}">
        <div class="rank">retention ${s.retention}/10 · impact ${s.visual_impact}/10</div>
        <img src="${s.thumbnail}" alt="">
        <div class="desc">${escapeHtml(s.description)}</div>
        <div class="meta">${escapeHtml(s.source_file)} · ${s.start.toFixed(1)}-${s.end.toFixed(1)}s</div>
      </div>
    `;
  }).join('');
  grid.querySelectorAll('[data-modal-scene]').forEach(el => {
    el.addEventListener('click', () => {
      selections[activeCutForModal] = el.dataset.modalScene;
      closeModal();
      render();
    });
  });
}

document.getElementById('modal-close').addEventListener('click', closeModal);
function closeModal() {
  document.getElementById('modal').classList.remove('open');
  activeCutForModal = null;
}
['filter-text', 'filter-file', 'filter-sort'].forEach(id => {
  document.getElementById(id).addEventListener('input', renderModalGrid);
});

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
  RECS.forEach(c => { selections[c.cut_index] = c.selected_scene_id; });
  render();
});

render();
</script>
</body>
</html>"""


def write_viewer_html(recs: list[dict], all_scenes: list[dict], output_dir: Path) -> Path:
    data_json = json.dumps({"recommendations": recs, "all_scenes": all_scenes}, ensure_ascii=False)
    html = _VIEWER_HTML.replace("__DATA__", data_json)
    path = output_dir / "viewer.html"
    path.write_text(html, encoding="utf-8")
    return path
