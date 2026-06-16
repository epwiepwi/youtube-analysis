"""Recommendations + fast-pick HTML viewer for human-in-the-loop editing.

The viewer's job is to let the user pick the right clip in ~2-3 minutes
total. Each candidate is shown as an auto-looping muted video preview
(not a still thumbnail) so action quality is obvious at a glance. Cut
captions sit above the row of candidates. Number keys 1-5 select a
candidate, → / Enter / Space advances to the next cut, ← goes back.
"확정" downloads selections.json and triggers PyQt to regenerate the
CapCut draft automatically.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .vision import ClipAnalysis


# Top-N candidates shown per cut. 5 fits cleanly in a row and keeps the
# total video-preview count under ~80 even for long videos.
CANDIDATES_PER_CUT = 5


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


def _extract_preview_video(source_file: Path, start: float, end: float,
                            out_path: Path) -> None:
    """Render a small looping preview (240p, no audio, ~3s) for one candidate.

    Auto-playing video makes action quality obvious — a frame that looks
    fine as a thumbnail might be a hand mid-motion that finishes awkwardly.
    Capped at 3 seconds so the loop tempo lets the user scan a row of
    candidates quickly.
    """
    if out_path.exists():
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dur = max(0.5, end - start)
    duration = min(raw_dur, 3.0)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.2f}",
            "-i", str(source_file),
            "-t", f"{duration:.2f}",
            "-vf", "scale='min(240,iw)':-2,fps=20",
            "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-movflags", "+faststart",
            str(out_path),
        ],
        check=True,
    )


def _safe_filename(scene_id: str) -> str:
    return scene_id.replace("#", "_at_").replace("/", "_").replace("\\", "_").replace(" ", "_")


def build_recommendations(
    cut_entries: list[dict],
    ranked_by_cut: dict[int, list[dict]],
    clips_analysis: dict[str, ClipAnalysis],
    output_dir: Path,
    extra_per_cut: int = CANDIDATES_PER_CUT - 3,
) -> tuple[list[dict], list[dict]]:
    """Build per-cut recommendations + a full scene library for browsing.

    Each cut surfaces up to CANDIDATES_PER_CUT candidates: the AI's top
    3 picks plus a few heuristic alternates from the rest of the pool.
    All candidates get a short looping video preview; the full library
    used by the "browse everything" modal sticks to single thumbnails so
    the page stays light.
    """
    from .matcher import _score_scene_for_plan  # local import to avoid cycle

    thumbs_dir = output_dir / "thumbs"
    previews_dir = output_dir / "previews"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    previews_dir.mkdir(parents=True, exist_ok=True)

    # Build the all-scenes library (thumbnails only — used by modal).
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

    # Collect the set of scenes that actually need a preview video so we
    # don't render motion previews for the entire library.
    preview_targets: set[str] = set()
    recs_skeleton: list[tuple[dict, list[dict]]] = []
    for entry in cut_entries:
        idx = entry["index"]
        picks = ranked_by_cut.get(idx, [])

        ai_picks: list[dict] = []
        seen: set[str] = set()
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
            seen.add(sid)
            preview_targets.add(sid)

        plan_surrogate = {
            "visual_intent": entry.get("reference_target_visual", "") or entry.get("owning_sentence_text", ""),
            "required_elements": entry.get("reference_target_elements", []),
            "forbidden_elements": [],
            "visual_phase": entry.get("reference_target_phase") or entry.get("phase", ""),
        }
        scored_extras = []
        for sid, a in clips_analysis.items():
            if sid in seen:
                continue
            scored_extras.append((_score_scene_for_plan(a, plan_surrogate), sid))
        scored_extras.sort(reverse=True)

        extras: list[dict] = []
        for score, sid in scored_extras[:extra_per_cut]:
            card = dict(scene_cards[sid])
            card.update({
                "rank": None,
                "score": round(score, 1),
                "reason": "heuristic 대안",
                "source": "heuristic",
            })
            extras.append(card)
            preview_targets.add(sid)

        recs_skeleton.append((entry, ai_picks + extras))

    # Now render motion previews only for the ~80 candidates we'll show.
    if preview_targets:
        print(f"  Rendering {len(preview_targets)} candidate preview videos...")
    preview_by_sid: dict[str, str] = {}
    for sid in preview_targets:
        a = clips_analysis[sid]
        name = f"{_safe_filename(sid)}.mp4"
        path = previews_dir / name
        try:
            _extract_preview_video(Path(a.source_file), a.start, a.end, path)
            preview_by_sid[sid] = f"previews/{name}"
        except Exception as e:
            preview_by_sid[sid] = ""
            print(f"    preview failed for {sid}: {e}")

    recs: list[dict] = []
    for entry, cands in recs_skeleton:
        for card in cands:
            card["preview"] = preview_by_sid.get(card["scene_id"], "")
        idx = entry["index"]
        recs.append({
            "cut_index": idx,
            "time": entry.get("time", ""),
            "phase": entry.get("phase", ""),
            "owning_sentence_index": entry.get("owning_sentence_index"),
            "owning_sentence_text": entry.get("owning_sentence_text", ""),
            "spoken_here": entry.get("spoken_here", ""),
            "reference_target_visual": entry.get("reference_target_visual", ""),
            "candidates": cands,
            "selected_scene_id": cands[0]["scene_id"] if cands else None,
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
<title>빠른 컷 고르기</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #0a0c10; color: #eee;
         margin: 0; padding: 0; }
  .topbar { position: sticky; top: 0; z-index: 20; background: #0a0c10; border-bottom: 1px solid #1f232c;
            padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  .topbar h1 { margin: 0; font-size: 16px; }
  .topbar .progress { color: #8b93a3; font-size: 13px; }
  .topbar button { background: #4ade80; color: #0a0c10; border: 0; padding: 10px 20px;
                   border-radius: 8px; font-weight: 700; font-size: 14px; cursor: pointer; }
  .topbar button:hover { background: #22c55e; }
  .topbar button.ghost { background: #1c2029; color: #e6e6e6; }
  .topbar .keys { font-size: 11px; color: #8b93a3; padding: 4px 8px; background: #1c2029;
                   border-radius: 6px; }
  .container { padding: 24px; max-width: 1400px; margin: 0 auto; }
  .cut { background: #15181f; border-radius: 14px; padding: 18px; margin-bottom: 16px;
         scroll-margin-top: 80px; border: 2px solid transparent; transition: border-color .15s; }
  .cut.active { border-color: #4ade80; }
  .cut.done { opacity: 0.55; }
  .cut header { margin-bottom: 12px; }
  .badges { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
  .badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; background: #1c2029; color: #bbb; }
  .badge.phase { background: #2a2f3c; }
  .caption-big { font-size: 17px; font-weight: 600; color: #fff; margin: 4px 0 2px; line-height: 1.4; }
  .caption-spoken { font-size: 13px; color: #4ade80; font-weight: 500; margin-bottom: 2px; }
  .caption-ref { font-size: 11px; color: #8aa; font-style: italic; }
  .row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
  .candidate { background: #1c2029; border-radius: 10px; padding: 8px; cursor: pointer;
               border: 2px solid transparent; position: relative; transition: border-color .15s, transform .1s; }
  .candidate:hover { border-color: #555; transform: translateY(-1px); }
  .candidate.selected { border-color: #4ade80; background: #1e3a26; }
  .candidate video, .candidate img { width: 100%; aspect-ratio: 9/16; object-fit: cover;
                                       border-radius: 6px; background: #0a0c10; display: block; }
  .hotkey { position: absolute; top: 4px; left: 4px; background: rgba(0,0,0,.7); color: #fff;
            border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 700; }
  .ai-tag { position: absolute; top: 4px; right: 4px; background: rgba(74,222,128,.85); color: #000;
            border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700; }
  .cand-desc { font-size: 11px; color: #ccc; line-height: 1.3; margin-top: 6px; max-height: 3em;
               overflow: hidden; }
  .cand-meta { font-size: 10px; color: #777; margin-top: 2px; }
  .browse-link { color: #8aa; font-size: 12px; cursor: pointer; margin-top: 8px; display: inline-block; }
  .browse-link:hover { color: #4ade80; }
  .modal { position: fixed; inset: 0; background: rgba(0,0,0,.9); z-index: 50; display: none;
           padding: 20px; overflow-y: auto; }
  .modal.open { display: block; }
  .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .modal-head input { flex: 1; background: #1c2029; border: 1px solid #2a2f3c; color: #eee;
                       padding: 8px 12px; border-radius: 6px; margin: 0 12px; }
  .modal-head button { background: #1c2029; color: #eee; border: 0; padding: 8px 16px;
                        border-radius: 6px; cursor: pointer; }
  .modal-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
  .modal-grid .card { background: #1c2029; border-radius: 8px; padding: 8px; cursor: pointer;
                       border: 2px solid transparent; }
  .modal-grid .card:hover { border-color: #555; }
  .modal-grid .card.selected { border-color: #4ade80; }
  .modal-grid img { width: 100%; aspect-ratio: 9/16; object-fit: cover; border-radius: 4px; }
  .modal-grid .desc { font-size: 11px; color: #ccc; margin-top: 4px; max-height: 3em; overflow: hidden; }
  #toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
           background: #1e3a26; color: #4ade80; padding: 10px 18px; border-radius: 8px;
           font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
  <div class="topbar">
    <h1>빠른 컷 고르기</h1>
    <div class="progress" id="progress">0/0 완료</div>
    <div class="keys">단축키: 1-5 선택 · → 다음 · ← 이전 · Enter 확정</div>
    <div style="flex:1"></div>
    <button class="ghost" id="reset">전부 1순위로</button>
    <button id="confirm">✓ 확정 → CapCut 생성</button>
  </div>

  <div class="container" id="root"></div>

  <div class="modal" id="modal">
    <div class="modal-head">
      <button id="modal-close">닫기 ✕</button>
      <input type="text" id="filter-text" placeholder="설명/태그 검색">
      <button id="modal-done">선택 안 함</button>
    </div>
    <div class="modal-grid" id="modal-grid"></div>
  </div>

  <div id="toast"></div>

<script>
const DATA = __DATA__;
const RECS = DATA.recommendations;
const ALL = DATA.all_scenes;
const selections = {};
RECS.forEach(c => { selections[c.cut_index] = c.selected_scene_id; });

let activeIndex = 0;
let modalCut = null;
const userPicked = new Set(); // cuts the user explicitly chose

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

function updateProgress() {
  document.getElementById('progress').textContent = `${userPicked.size}/${RECS.length} 확정`;
}

function render() {
  const root = document.getElementById('root');
  root.innerHTML = RECS.map((cut, idx) => {
    const cards = cut.candidates.slice(0, 5).map((c, ci) => {
      const isSel = selections[cut.cut_index] === c.scene_id;
      const tag = c.source === 'ai' ? `<span class="ai-tag">AI #${c.rank || '?'}</span>` : '';
      const media = c.preview
        ? `<video src="${c.preview}" autoplay loop muted playsinline preload="metadata"></video>`
        : (c.thumbnail ? `<img src="${c.thumbnail}" alt="">` : '<div style="aspect-ratio:9/16;background:#222"></div>');
      return `
        <div class="candidate ${isSel ? 'selected' : ''}" data-cut="${cut.cut_index}" data-scene="${c.scene_id}" data-idx="${ci}">
          <div class="hotkey">${ci + 1}</div>${tag}
          ${media}
          <div class="cand-desc">${escapeHtml(c.description)}</div>
          <div class="cand-meta">${escapeHtml(c.source_file)} · ${c.start.toFixed(1)}-${c.end.toFixed(1)}s · ret ${c.retention}/10</div>
        </div>
      `;
    }).join('');
    const done = userPicked.has(cut.cut_index);
    return `
      <div class="cut ${idx === activeIndex ? 'active' : ''} ${done ? 'done' : ''}" data-cut-row="${cut.cut_index}" id="cut-${cut.cut_index}">
        <header>
          <div class="badges">
            <span class="badge">컷 ${cut.cut_index + 1}/${RECS.length}</span>
            <span class="badge phase">${cut.phase || ''}</span>
            <span class="badge">${cut.time || ''}</span>
            ${done ? '<span class="badge" style="background:#1e3a26;color:#4ade80;">✓ 확정</span>' : ''}
          </div>
          <div class="caption-big">${escapeHtml(cut.owning_sentence_text)}</div>
          ${cut.spoken_here && cut.spoken_here !== cut.owning_sentence_text
            ? `<div class="caption-spoken">▶ "${escapeHtml(cut.spoken_here)}"</div>` : ''}
          ${cut.reference_target_visual ? `<div class="caption-ref">🎯 참조: ${escapeHtml(cut.reference_target_visual)}</div>` : ''}
        </header>
        <div class="row">${cards}</div>
        <a class="browse-link" data-open-pool="${cut.cut_index}">전체 풀에서 직접 고르기 →</a>
      </div>
    `;
  }).join('');

  document.querySelectorAll('.candidate').forEach(el => {
    el.addEventListener('click', () => {
      const cutIdx = Number(el.dataset.cut);
      pickFor(cutIdx, el.dataset.scene, true);
    });
  });
  document.querySelectorAll('[data-open-pool]').forEach(el => {
    el.addEventListener('click', () => openModal(Number(el.dataset.openPool)));
  });
  updateProgress();
}

function pickFor(cutIdx, sceneId, advance) {
  selections[cutIdx] = sceneId;
  userPicked.add(cutIdx);
  render();
  if (advance) {
    setTimeout(() => goNext(), 120);
  }
}

function goToIndex(i) {
  if (i < 0 || i >= RECS.length) return;
  activeIndex = i;
  const cut = RECS[i];
  const el = document.getElementById('cut-' + cut.cut_index);
  if (el) {
    el.scrollIntoView({behavior: 'smooth', block: 'start'});
    document.querySelectorAll('.cut').forEach(c => c.classList.remove('active'));
    el.classList.add('active');
  }
}

function goNext() {
  if (activeIndex < RECS.length - 1) goToIndex(activeIndex + 1);
}

function goPrev() {
  if (activeIndex > 0) goToIndex(activeIndex - 1);
}

document.addEventListener('keydown', e => {
  if (document.getElementById('modal').classList.contains('open')) return;
  if (e.target.tagName === 'INPUT') return;
  const cut = RECS[activeIndex];
  if (!cut) return;
  if (e.key >= '1' && e.key <= '5') {
    const ci = parseInt(e.key, 10) - 1;
    const cand = cut.candidates[ci];
    if (cand) {
      e.preventDefault();
      pickFor(cut.cut_index, cand.scene_id, true);
    }
  } else if (e.key === 'ArrowRight' || e.key === ' ') {
    e.preventDefault();
    goNext();
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault();
    goPrev();
  } else if (e.key === 'Enter') {
    if (userPicked.size === RECS.length) {
      e.preventDefault();
      confirmAll();
    } else {
      e.preventDefault();
      goNext();
    }
  }
});

function openModal(cutIdx) {
  modalCut = cutIdx;
  renderModal();
  document.getElementById('modal').classList.add('open');
}

function renderModal() {
  const filter = document.getElementById('filter-text').value.toLowerCase().trim();
  let list = ALL;
  if (filter) {
    list = list.filter(s => (s.description + ' ' + (s.tags || []).join(' ')).toLowerCase().includes(filter));
  }
  const grid = document.getElementById('modal-grid');
  grid.innerHTML = list.map(s => {
    const isSel = selections[modalCut] === s.scene_id;
    return `
      <div class="card ${isSel ? 'selected' : ''}" data-modal-scene="${s.scene_id}">
        <img src="${s.thumbnail}" alt="">
        <div class="desc">${escapeHtml(s.description)}</div>
        <div class="cand-meta">${escapeHtml(s.source_file)} · ${s.start.toFixed(1)}-${s.end.toFixed(1)}s</div>
      </div>
    `;
  }).join('');
  grid.querySelectorAll('[data-modal-scene]').forEach(el => {
    el.addEventListener('click', () => {
      pickFor(modalCut, el.dataset.modalScene, true);
      closeModal();
    });
  });
}

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-done').addEventListener('click', closeModal);
document.getElementById('filter-text').addEventListener('input', renderModal);
function closeModal() {
  document.getElementById('modal').classList.remove('open');
  modalCut = null;
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 1800);
}

function confirmAll() {
  // Mark every cut as picked when the user confirms (covers ones they
  // skipped — they're implicitly accepting AI #1 for those).
  RECS.forEach(c => userPicked.add(c.cut_index));
  const payload = {};
  Object.entries(selections).forEach(([k, v]) => { payload[`cut_${k}`] = v; });
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'selections.json';
  a.click();
  showToast('확정! CapCut 재생성 시작합니다...');
}

document.getElementById('confirm').addEventListener('click', confirmAll);
document.getElementById('reset').addEventListener('click', () => {
  RECS.forEach(c => { selections[c.cut_index] = c.selected_scene_id; });
  userPicked.clear();
  render();
});

render();
goToIndex(0);
</script>
</body>
</html>"""


def write_viewer_html(recs: list[dict], all_scenes: list[dict], output_dir: Path) -> Path:
    data_json = json.dumps({"recommendations": recs, "all_scenes": all_scenes}, ensure_ascii=False)
    html = _VIEWER_HTML.replace("__DATA__", data_json)
    path = output_dir / "viewer.html"
    path.write_text(html, encoding="utf-8")
    return path
