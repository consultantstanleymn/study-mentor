(() => {
  const NAMES = { aws: "AWS Pro", lsat: "LSAT", quant: "Quant" };
  const rows = document.getElementById("remindRows");
  const preview = document.getElementById("crabPreview");
  if (!rows || !preview) return;
  let sched = null;

  const save = async () => {
    try {
      const r = await fetch("/api/schedule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(sched) });
      msg.textContent = r.ok ? "Saved" : "Could not save";
    } catch { msg.textContent = "Could not save"; }
  };

  const msg = document.createElement("div");
  msg.className = "remind-msg"; msg.setAttribute("role", "status");
  preview.after(msg);

  const render = () => {
    rows.textContent = "";
    sched.reminders.forEach((rem) => {
      const row = document.createElement("div");
      row.className = "remind-row";
      const lab = document.createElement("label");
      lab.className = "chk";
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = rem.enabled;
      cb.addEventListener("change", () => { rem.enabled = cb.checked; save(); });
      lab.append(cb, NAMES[rem.track] || rem.track);
      const t = document.createElement("input");
      t.type = "time"; t.value = rem.time; t.setAttribute("aria-label", `${NAMES[rem.track] || rem.track} reminder time`);
      t.addEventListener("change", () => { if (t.value) { rem.time = t.value; save(); } });
      row.append(lab, t);
      rows.append(row);
    });
  };

  fetch("/api/schedule").then((r) => r.json()).then((d) => { sched = d; render(); }).catch(() => { msg.textContent = "Schedule unavailable"; });

  preview.addEventListener("click", () => {
    const first = sched && (sched.reminders.find((r) => r.enabled) || sched.reminders[0]);
    fetch("/api/crab-test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ track: first ? first.track : "aws" }) }).catch(() => {});
  });
})();
