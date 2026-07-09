// time-capsule-cam — pairs .mp4 videos with their .wav rows in the recordings table.
// The upstream table is built by recordings.js from /api/recordings, which lists
// every file in the folder: we hide our .mp4/.jpg rows and attach a <video>
// player to the matching audio row instead.
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

  function enhance() {
    var rows = Array.prototype.slice.call(list.querySelectorAll("tr[data-filename]"));
    var mp4s = [];
    var jpgs = {};

    rows.forEach(function (row) {
      var name = row.dataset.filename;
      if (/\.mp4$/i.test(name)) {
        row.style.display = "none";
        mp4s.push({ name: name, t: ts(name) });
      } else if (/\.jpg$/i.test(name)) {
        row.style.display = "none";
        jpgs[name] = true;
      }
    });

    rows.forEach(function (row) {
      var name = row.dataset.filename;
      if (!/\.wav$/i.test(name) || row.querySelector(".tcc-video")) return;
      var tw = ts(name);
      if (tw === null) return; // renamed recording — can't pair

      // latest mp4 that started before this wav (3 min covers any greeting)
      var best = null;
      mp4s.forEach(function (v) {
        var d = v.t === null ? null : tw - v.t;
        if (d !== null && d >= -5 && d <= 180 && (!best || v.t > best.t)) best = v;
      });
      if (!best) return;

      var video = document.createElement("video");
      video.className = "tcc-video";
      video.controls = true;
      video.preload = "none"; // don't hammer the Pi loading every video
      video.src = "/recordings/" + encodeURIComponent(best.name);
      var poster = best.name.replace(/\.mp4$/i, ".jpg");
      if (jpgs[poster]) video.poster = "/recordings/" + encodeURIComponent(poster);
      video.style.cssText = "margin-top:.5rem;width:16rem;max-width:100%;border-radius:.375rem;display:block;";
      // upstream row click toggles selection — don't let player clicks do that
      video.addEventListener("click", function (e) { e.stopPropagation(); });

      var audio = row.querySelector("audio, .plyr");
      var cell = audio ? audio.closest("td") : row.cells[2];
      if (cell) cell.appendChild(video);
    });
  }

  new MutationObserver(enhance).observe(list, { childList: true });
  enhance();
})();
