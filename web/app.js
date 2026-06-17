const $ = (id) => document.getElementById(id);
let SESSION = null;
let SEGMENTS = [];

async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  return r.json();
}

function fmt(t) {
  const m = Math.floor(t / 60), s = (t % 60).toFixed(1).padStart(4, "0");
  return `${m}:${s}`;
}

// ---- 상태 ----
(async () => {
  try {
    const s = await (await fetch("/api/status")).json();
    const llm = s.llm ? `<span class="ok">LLM:${s.llm_provider}✓</span>` : `<span class="off">LLM:키없음(목업)</span>`;
    const tts = s.tts ? `<span class="ok">TTS✓</span>` : `<span class="off">TTS:키없음(목업)</span>`;
    const cap = s.capcut_dir ? `<span class="ok">캡컷경로✓</span>` : `<span class="off">캡컷경로:미설정(output에 저장)</span>`;
    $("status").innerHTML = `${llm} · ${tts} · ${cap}`;
  } catch { $("status").textContent = "서버 연결 실패"; }
})();

// ---- 1. 대본 ----
$("gen-script").onclick = async (e) => {
  const btn = e.target; btn.disabled = true; btn.textContent = "생성 중…";
  try {
    const res = await jpost("/api/script", {
      source: $("src").value, product: $("product").value, extra: $("extra").value,
    });
    $("script").value = res.script;
    if (res.mode === "mock") alert("LLM 키가 없어 목업으로 정리했어요. .env에 키를 넣으면 진짜 공감형 대본이 나옵니다.");
  } catch (err) { alert("대본 오류: " + err.message); }
  finally { btn.disabled = false; btn.textContent = "✨ 공감형 대본 생성"; }
};

$("use-as-is").onclick = () => {
  if (!$("script").value.trim() && $("src").value.trim()) $("script").value = $("src").value;
  $("script").focus();
};

// ---- 2. TTS ----
$("gen-tts").onclick = async (e) => {
  const script = $("script").value.trim();
  if (!script) { alert("먼저 대본을 채우세요."); return; }
  const btn = e.target; btn.disabled = true; btn.textContent = "음성 생성 중…";
  try {
    const res = await jpost("/api/tts", { script, use_intro: $("use-intro").checked });
    SESSION = res.session_id; SEGMENTS = res.segments;
    const a = $("audio"); a.src = res.audio_url + "?t=" + Date.now(); a.hidden = false;
    $("tts-info").innerHTML =
      `길이 ${res.duration}s · 버리는 멘트 ${res.intro_offset}s 제외 · ${res.mode === "mock" ? "⚠️ 목업(더미 음성)" : "실제 음성"}`;
    renderSegments();
  } catch (err) { alert("TTS 오류: " + err.message); }
  finally { btn.disabled = false; btn.textContent = "🔊 음성 생성"; }
};

// ---- 3. 자막 ----
function renderSegments() {
  const box = $("segments"); box.innerHTML = "";
  SEGMENTS.forEach((seg, i) => {
    const div = document.createElement("div");
    div.className = "seg";
    div.innerHTML = `<span class="t">${fmt(seg.start)}→${fmt(seg.end)}</span>`;
    const inp = document.createElement("input");
    inp.value = seg.text;
    inp.oninput = () => { SEGMENTS[i].text = inp.value; };
    div.appendChild(inp);
    box.appendChild(div);
  });
}

// ---- 4. 내보내기 ----
$("export").onclick = async (e) => {
  if (!SESSION) { alert("먼저 TTS를 생성하세요."); return; }
  const btn = e.target; btn.disabled = true; btn.textContent = "생성 중…";
  try {
    const res = await jpost("/api/export", { session_id: SESSION, segments: SEGMENTS });
    const cap = res.capcut.ok
      ? `<span class="info good">✅ 캡컷 드래프트 생성: ${res.capcut.path}\n캡컷에서 이 프로젝트 열고 메인 영상만 올리면 됩니다.</span>`
      : `<span class="info bad">⚠️ 캡컷 드래프트 건너뜀: ${res.capcut.error}</span>`;
    $("export-result").innerHTML =
      `<a class="ghost" href="${res.srt_url}" download>⬇ SRT 다운로드</a>\n` +
      `오디오: ${res.audio_path}\n` + cap;
  } catch (err) { alert("내보내기 오류: " + err.message); }
  finally { btn.disabled = false; btn.textContent = "📦 SRT + 캡컷 드래프트 생성"; }
};
