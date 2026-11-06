/* Drive the upload sidebar in headless Chrome and report the finished page state. */
const {spawn} = require("child_process");
const fs = require("fs");
const http = require("http");

const baseUrl = process.argv[2];
const filePath = process.argv[3];
const outPath = process.argv[4];
const debugPort = Number(process.argv[5]);
const userDir = process.argv[6];
const screenshotPath = process.argv[7] || "";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getJson(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let body = "";
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); }
        catch (err) { reject(err); }
      });
    });
    req.on("error", reject);
  });
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const pending = new Map();
    let seq = 0;
    ws.addEventListener("open", () => resolve({ws, pending, nextId: () => ++seq}));
    ws.addEventListener("error", () => reject(new Error("Chrome debugger socket failed")));
    ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const waiter = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) waiter.reject(new Error(JSON.stringify(msg.error)));
        else waiter.resolve(msg.result || {});
      }
    });
  });
}

async function main() {
  const chrome = process.env.CHROME_BIN || "google-chrome";
  const proc = spawn(chrome, [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${userDir}`,
    "--window-size=1280,900",
    "about:blank",
  ], {stdio: "ignore"});

  let browser = null;
  try {
    let version = null;
    for (let i = 0; i < 50; i++) {
      try {
        version = await getJson(`http://127.0.0.1:${debugPort}/json/version`);
        break;
      } catch (err) {
        await sleep(100);
      }
    }
    if (!version) throw new Error("Chrome did not open a debugger port");
    browser = await connect(version.webSocketDebuggerUrl);
    const send = (method, params, sessionId) => new Promise((resolve, reject) => {
      const id = browser.nextId();
      browser.pending.set(id, {resolve, reject});
      const payload = {id, method, params: params || {}};
      if (sessionId) payload.sessionId = sessionId;
      browser.ws.send(JSON.stringify(payload));
    });

    const created = await send("Target.createTarget", {url: "about:blank"});
    const attached = await send("Target.attachToTarget", {targetId: created.targetId, flatten: true});
    const sessionId = attached.sessionId;
    const page = (method, params) => send(method, params, sessionId);

    await page("Page.enable");
    await page("Runtime.enable");
    await page("DOM.enable");
    await page("Page.setDownloadBehavior", {behavior: "allow", downloadPath: userDir});
    await page("Page.navigate", {url: baseUrl});

    let ready = null;
    for (let i = 0; i < 80; i++) {
      const result = await page("Runtime.evaluate", {
        expression: "JSON.stringify({ready: document.readyState, form: !!document.querySelector('form.progress-form'), api: !!window.mtRecentExports})",
        returnByValue: true,
      });
      ready = JSON.parse(result.result.value);
      if (ready.form && ready.api) break;
      await sleep(250);
    }
    if (!ready || !ready.form || !ready.api) {
      throw new Error("Upload form was not ready: " + JSON.stringify(ready));
    }

    await page("Runtime.evaluate", {expression: "window.__mtPageMark='still-here'", returnByValue: true});
    const before = JSON.parse((await page("Runtime.evaluate", {
      expression: snapshotExpression(),
      returnByValue: true,
    })).result.value);

    const doc = await page("DOM.getDocument", {depth: 0});
    const found = await page("DOM.querySelector", {nodeId: doc.root.nodeId, selector: "#files"});
    if (!found.nodeId) throw new Error("File input was not found");
    await page("DOM.setFileInputFiles", {nodeId: found.nodeId, files: [filePath]});
    const submitted = await page("Runtime.evaluate", {
      expression: "(function(){var f=document.querySelector('form.progress-form'); f.requestSubmit(); return document.querySelector('#files').files.length;})()",
      returnByValue: true,
    });
    if (!submitted.result.value) throw new Error("The file was not attached to the form");

    let after = null;
    for (let i = 0; i < 80; i++) {
      after = JSON.parse((await page("Runtime.evaluate", {
        expression: snapshotExpression(),
        returnByValue: true,
      })).result.value);
      const listed = (after.links || []).some((link) => link.text.indexOf("latest.xlsx") !== -1);
      const settled = after.message && after.message.indexOf("Downloading") === -1 && after.message.indexOf("Screened") !== -1;
      if (listed && settled && !after.progressHidden && after.mark === "still-here") break;
      if (after.error) break;
      await sleep(500);
    }

    let shot = "";
    if (screenshotPath) {
      const image = await page("Page.captureScreenshot", {format: "png"});
      fs.writeFileSync(screenshotPath, Buffer.from(image.data, "base64"));
      shot = screenshotPath;
    }
    const report = {ok: false, before, after, screenshot: shot};
    const links = after.links || [];
    const latest = links.findIndex((link) => link.text.indexOf("latest.xlsx") !== -1);
    const earlier = links.findIndex((link) => link.text.indexOf("earlier.xlsx") !== -1);
    report.ok = !after.error
      && after.mark === "still-here"
      && after.path.indexOf("/dashboard") !== -1
      && !after.progressHidden
      && !after.messageHidden
      && after.message.indexOf("Screened") !== -1
      && after.message.indexOf("Downloading") === -1
      && after.stage === "Done"
      && !after.cardHidden
      && latest === 0
      && earlier > latest
      && links.filter((link) => link.text.indexOf("latest.xlsx") !== -1).length === 1
      && !!links[latest].href && links[latest].href.indexOf("/download") !== -1;
    fs.writeFileSync(outPath, JSON.stringify(report, null, 2));
    if (!report.ok) {
      process.stderr.write(JSON.stringify(report, null, 2) + "\n");
      process.exitCode = 1;
    }
  } finally {
    if (browser) browser.ws.close();
    proc.kill("SIGKILL");
  }
}

function snapshotExpression() {
  return `(function(){
    var card=document.getElementById("recent-downloads");
    var msg=document.querySelector(".presult");
    var err=document.querySelector(".perror");
    var box=document.querySelector(".progress");
    var stage=document.querySelector(".pstage");
    var links=[];
    if(card){
      card.querySelectorAll("a").forEach(function(a){
        links.push({href:a.getAttribute("href"), text:a.textContent});
      });
    }
    return JSON.stringify({
      path: location.pathname,
      mark: window.__mtPageMark || "",
      cardHidden: !card || card.hidden,
      progressHidden: !box || box.hidden,
      messageHidden: !msg || msg.hidden,
      message: msg ? msg.textContent : "",
      error: err && !err.hidden ? err.textContent : "",
      stage: stage ? stage.textContent : "",
      links: links
    });
  })()`;
}

main().catch((err) => {
  try {
    fs.writeFileSync(outPath, JSON.stringify({ok: false, error: String(err && err.stack || err)}));
  } catch (writeErr) { /* the pytest assertion reports the missing file */ }
  process.stderr.write(String(err && err.stack || err) + "\n");
  process.exit(1);
});
