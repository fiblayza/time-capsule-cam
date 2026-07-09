// time-capsule-cam — pairs .mp4 videos with their .wav rows in the recordings table.
// The upstream table is built by recordings.js from /api/recordings, which lists
// every file in the folder: we hide our .jpg rows, attach each paired video to
// its audio row, and leave audio-less videos visible as their own rows.
//
// Pairing is fuzzy on purpose: the video starts at off-hook, but upstream names
// the .wav when recording starts — after the greeting has played. So the wav
// timestamp is always LATER than the mp4 one, by up to the greeting length.
(function () {
  var list = document.getElementById("recording-list");
  if (!list) return;

  // wav: 2024-06-15T17:32:04.123456.wav  /  mp4: 2024-06-15_17-32-04.mp4
  function ts(name) {
    var m = name.match(/(\d{4})-(\d{2})-(\d{2})[T_](\d{2})[:\-](\d{2})[:\-](\d{2})/);
    return m ? Date.UTC(m[1], m[2] - 1, m[3], m[4], m[5], m[6]) / 1000 : null;
  }

  function attachVideo(row, name, jpgs) {
    var video = document.createElement("video");
    video.className = "tcc-video";
    video.controls = true;
    video.preload = "none"; // don't hammer the Pi loading every video
    video.src = "/recordings/" + encodeURIComponent(name);
    var poster = name.replace(/\.mp4$/i, ".jpg");
    if (jpgs[poster]) video.poster = "/recordings/" + encodeURIComponent(poster);
    video.style.cssText = "margin-top:.5rem;width:16rem;max-width:100%;border-radius:.375rem;display:block;";
    // upstream row click toggles selection — don't let player clicks do that
    video.addEventListener("click", function (e) { e.stopPropagation(); });

    var audio = row.querySelector("audio, .plyr");
    var cell = audio ? audio.closest("td") : row.cells[2];
    if (cell) cell.appendChild(video);
  }

  function enhance() {
    var rows = Array.prototype.slice.call(list.querySelectorAll("tr[data-filename]"));
    var mp4s = [];
    var jpgs = {};

    rows.forEach(function (row) {
      var name = row.dataset.filename;
      if (/\.mp4$/i.test(name)) {
        mp4s.push({ name: name, t: ts(name), row: row });
      } else if (/\.jpg$/i.test(name)) {
        row.style.display = "none";
        jpgs[name] = true;
      }
    });

    var claimed = {};
    rows.forEach(function (row) {
      var name = row.dataset.filename;
      if (!/\.wav$/i.test(name)) return;
      var tw = ts(name);
      if (tw === null) return; // renamed recording — can't pair

      // latest mp4 that started before this wav (3 min covers any greeting)
      var best = null;
      mp4s.forEach(function (v) {
        var d = v.t === null ? null : tw - v.t;
        if (d !== null && d >= -5 && d <= 180 && (!best || v.t > best.t)) best = v;
      });
      if (!best) return;
      claimed[best.name] = true;
      if (!row.querySelector(".tcc-video")) attachVideo(row, best.name, jpgs);
    });

    mp4s.forEach(function (v) {
      if (claimed[v.name]) {
        v.row.style.display = "none"; // shown inside its audio row instead
        return;
      }
      // video-only session (no audio recorded): keep its own row visible
      v.row.style.display = "";
      if (!v.row.querySelector(".tcc-video")) {
        var broken = v.row.querySelector("audio, .plyr");
        if (broken) broken.style.display = "none"; // upstream's player can't play mp4-in-audio
        attachVideo(v.row, v.name, jpgs);
      }
    });
  }

  new MutationObserver(enhance).observe(list, { childList: true });
  enhance();
})();
