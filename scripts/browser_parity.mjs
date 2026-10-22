#!/usr/bin/env node
// Opt-in local OFFLINE DEMO acceptance. Needs Playwright + installed browsers.
// PLAYWRIGHT_PROJECT may name a directory containing Playwright's package.json.
// node scripts/browser_parity.mjs http://127.0.0.1:8793 /path/to/demo-home/web-token /tmp/evidence
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

let child;
async function main() {
let [base, tokenFile, evidence] = process.argv.slice(2);
if (base === "--spawn") {
  evidence = tokenFile;
  if (!evidence) throw new Error("Usage: browser_parity.mjs --spawn EVIDENCE_DIR");
  const folder = await mkdtemp(resolve(tmpdir(), "eternalchat-browser-"));
  tokenFile = resolve(folder, "home", "web-token");
  child = spawn("python3", ["-u", "-m", "eternalchat", "--demo", "--tools", "off", "--data-dir",
    resolve(folder, "chat"), "web", "--port", "0", "--no-browser"], {
    env: {...process.env, ETERNALCHAT_HOME: resolve(folder, "home")}, stdio: ["ignore", "pipe", "pipe"]
  });
  base = await new Promise((yes, no) => {
    const timer = setTimeout(() => no(new Error("Demo server startup timed out")), 10000);
    let text = "";
    child.stdout.on("data", (data) => {
      text += data;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timer); yes(match[0]); }
    });
    child.once("error", (error) => { clearTimeout(timer); no(error); });
    child.once("exit", () => { clearTimeout(timer); no(new Error("Demo server exited before startup")); });
  });
}
if (!base || !tokenFile || !evidence || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) {
  throw new Error("Usage: browser_parity.mjs http://127.0.0.1:PORT DEMO_TOKEN_FILE EVIDENCE_DIR");
}
const require = createRequire(pathToFileURL(resolve(process.env.PLAYWRIGHT_PROJECT || ".", "package.json")));
const { chromium, webkit } = require("playwright");
const token = (await readFile(tokenFile, "utf8")).trim();
const headers = { "X-EternalChat-Token": token };
const state = await fetch(base + "/api/state", { headers }).then((r) => r.json());
assert.equal(state.settings.demo, true, "Use a disposable offline demo server only");
assert.equal(state.status.messages, 0, "Use a fresh empty demo chat");
await mkdir(evidence, { recursive: true });
const date = new Date().toISOString();
const secret = "sk-ant-" + "a1B2".repeat(10);
const data = [
  {type: "user", cwd: "/demo/parity", timestamp: date,
    message: {content: "browser import " + secret}},
  {type: "assistant", timestamp: date,
    message: {content: [{type: "text", text: "browser import answer"}]}}
].map((r) => JSON.stringify(r)).join("\n");
const errors = [];
const report = { flows: [], layouts: [], browsers: {}, errors };
for (const [name, browserType] of [["chromium", chromium], ["webkit", webkit]].filter(([name])=>!process.env.PARITY_BROWSER || name === process.env.PARITY_BROWSER)) {
  const browser = await browserType.launch({ headless: true });
  report.browsers[name] = browser.version();
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true, serviceWorkers: "block", reducedMotion: "reduce" });
    await context.addInitScript(()=>{
      let subscription = null;
      window.__pushUI = {subscribed:0,unsubscribed:0};
      const data = {endpoint:"https://fcm.googleapis.com/wp/offline-fixture",keys:{p256dh:"B"+"a".repeat(86),auth:"b".repeat(22)}};
      const registration = {pushManager:{
        getSubscription:async()=>subscription,
        subscribe:async options=>{
          if(!options.userVisibleOnly || options.applicationServerKey.length !== 65) throw new Error("Invalid PushManager options");
          window.__pushUI.subscribed++;
          subscription={toJSON:()=>data,unsubscribe:async()=>{window.__pushUI.unsubscribed++;subscription=null;return true;}};
          return subscription;
        }
      }};
      Object.defineProperty(navigator,"serviceWorker",{configurable:true,value:{ready:Promise.resolve(registration),register:async()=>registration,getRegistration:async()=>registration,addEventListener(){}}});
      Object.defineProperty(window,"PushManager",{configurable:true,value:function(){}});
      const notification = function(){}; notification.permission="default";
      notification.requestPermission=async()=>{notification.permission="granted";return "granted";};
      Object.defineProperty(window,"Notification",{configurable:true,value:notification});
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(name + ": " + error.message));
    // The backend remains offline; unlock only the settings UI for this check.
    await page.route("**/api/state", async (route) => {
      const response = await route.fetch();
      const value = await response.json();
      value.settings.demo = false;
      value.audio.configured = true; // UI-only audio fixture; backend stays demo.
      await route.fulfill({response, json: value});
    });
    let pushEnabled=false, pushTests=0;
    const pushPublic = Buffer.concat([Buffer.from([4]),Buffer.alloc(64,1)]).toString("base64url");
    await page.route("**/api/push",route=>route.fulfill({json:{configured:true,public_key:pushPublic,subscribed:pushEnabled}}));
    await page.route("**/api/push/subscribe",route=>{
      assert.equal(route.request().postDataJSON().endpoint,"https://fcm.googleapis.com/wp/offline-fixture");
      pushEnabled=true; return route.fulfill({json:{ok:true}});
    });
    await page.route("**/api/push/unsubscribe",route=>{pushEnabled=false;return route.fulfill({json:{ok:true}});});
    await page.route("**/api/push/test",route=>{pushTests++;return route.fulfill({json:{ok:true}});});
    await page.goto(base + "/#" + token);
    await page.waitForFunction(()=>localStorage.getItem("eternalchat-token")?.startsWith("ecs."));
    const browserToken = await page.evaluate(()=>localStorage.getItem("eternalchat-token"));
    assert.notEqual(browserToken, token);
    assert.equal(await page.evaluate(()=>location.hash), "");
    await page.getByRole("button", { name:"Settings", exact:true }).click();
    await page.waitForFunction(()=>document.querySelector("#notify-note").textContent.includes("Works when this page is closed"));
    await page.locator("#notify").check();
    await page.waitForFunction(()=>!document.querySelector("#push-test").hidden);
    assert.equal(pushEnabled,true);
    await page.locator("#push-test").click(); assert.equal(pushTests,1);
    await page.setViewportSize({width:375,height:900});
    await page.locator("#notify-section").scrollIntoViewIfNeeded();
    await page.screenshot({path:resolve(evidence,`${name}-push-375.png`),animations:"disabled"});
    await page.locator("#notify").uncheck();
    await page.waitForFunction(()=>document.querySelector("#push-test").hidden && !document.querySelector("#notify").disabled);
    assert.equal(pushEnabled,false);
    assert.deepEqual(await page.evaluate(()=>window.__pushUI),{subscribed:1,unsubscribed:1});
    report.flows.push(`${name}: opt-in permission/PushManager fixtures -> register -> test -> unregister; generic closed-page notification controls`);
    await page.setViewportSize({width:1280,height:900});
    await page.locator("#copy-link").click();
    await page.waitForFunction(()=>document.querySelector("#pairing-link").value.includes("#eci."));
    const invitation = await page.locator("#pairing-link").inputValue();
    assert.ok(!invitation.includes(browserToken));
    const paired = await browser.newContext({serviceWorkers:"block", reducedMotion:"reduce"});
    const other = await paired.newPage();
    await other.goto(invitation);
    await other.waitForFunction(()=>localStorage.getItem("eternalchat-token")?.startsWith("ecs."));
    const otherToken = await other.evaluate(()=>localStorage.getItem("eternalchat-token"));
    assert.notEqual(otherToken,browserToken);
    const secondSession = await fetch(base+"/api/sessions", {headers:{"X-EternalChat-Token":otherToken}}).then(r=>r.json());
    const reused = await paired.newPage();
    await reused.goto(invitation);
    await reused.locator("#locked").waitFor({state:"visible"});
    await reused.close();
    await page.locator("#settings [data-close]").click();
    await page.getByRole("button", { name:"Settings", exact:true }).click();
    const revoke = page.locator(`[data-session-id="${secondSession.current}"] button`);
    await revoke.waitFor();
    await page.setViewportSize({width:375,height:900});
    await revoke.scrollIntoViewIfNeeded();
    await page.screenshot({path:resolve(evidence,`${name}-sessions-375.png`),animations:"disabled"});
    await revoke.click();
    await other.locator("#locked").waitFor({state:"visible"});
    assert.equal((await fetch(base+"/api/state",{headers:{"X-EternalChat-Token":browserToken}})).status,200);
    await paired.close();
    await page.locator("#settings [data-close]").click();
    await page.setViewportSize({width:1280,height:900});
    report.flows.push(`${name}: bootstrap exchanged for independent session; single-use pairing; UI revocation disconnects only the selected browser`);
    const beforeAudio = await fetch(base + "/api/state", {headers}).then(r=>r.json());
    let audioCalls = 0;
    await page.route("**/api/transcribe", async route => {
      audioCalls++;
      assert.ok(route.request().headers()["content-type"].startsWith("audio/"));
      assert.match(route.request().headers()["x-eternalchat-token"], /^ecs\./);
      await route.fulfill({json:{text:"fixture voice draft", provider:"openai", model:"gpt-transcribe"}});
    });
    await page.getByRole("button", {name:"Voice input", exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector("#voice-file").disabled);
    const recordingFile = {name:"recording.webm", mimeType:"audio/webm", buffer:Buffer.from("offline audio fixture")};
    if (name === "chromium") {
      const audioChooser = page.waitForEvent("filechooser");
      await page.locator("#voice-file").click();
      await (await audioChooser).setFiles(recordingFile);
    } else {
      await page.locator("#voice-upload").setInputFiles(recordingFile);
    }
    await page.waitForFunction(()=>document.querySelector("#input").value === "fixture voice draft").catch(async error=>{
      console.log(JSON.stringify({browser:name, audioCalls, errors, diagnostics:await page.evaluate(()=>({open:document.querySelector("#voice").open, status:document.querySelector("#voice-status").textContent, toasts:document.querySelector("#toasts").textContent, draft:document.querySelector("#input").value}))}));
      throw error;
    });
    assert.equal((await fetch(base + "/api/state", {headers}).then(r=>r.json())).status.messages, beforeAudio.status.messages);
    assert.equal(audioCalls, 1);
    await page.locator("#input").fill("");
    // Exercise recorder lifecycle with an explicit browser fixture (no real microphone).
    await page.evaluate(()=>{
      window.__audioStopped = 0; window.__voiceDebug = [];
      document.querySelector("#voice-record").addEventListener("click",()=>window.__voiceDebug.push("click"),true);
      document.querySelector("#voice").addEventListener("close",()=>window.__voiceDebug.push("close"));
      Object.defineProperty(navigator.mediaDevices, "getUserMedia", {configurable:true, value:async()=>{window.__voiceDebug.push("getUserMedia");return {getTracks:()=>[{stop:()=>window.__audioStopped++}]};}});
      window.MediaRecorder = class {
        static isTypeSupported(){return true;}
        constructor(){window.__voiceDebug.push("construct");this.mimeType="audio/webm";this.state="inactive";}
        start(){window.__voiceDebug.push("start");this.state="recording";}
        stop(){window.__voiceDebug.push("stop:"+new Error().stack);this.state="inactive";this.ondataavailable({data:new Blob(["fixture"],{type:this.mimeType})});this.onstop();}
      };
    });
    await page.getByRole("button", {name:"Voice input", exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector("#voice-record").disabled);
    await page.locator("#voice-record").evaluate(button=>button.click());
    await page.waitForFunction(()=>document.querySelector("#voice-record").textContent === "Stop and transcribe").catch(async error=>{
      console.log(JSON.stringify({browser:name, errors, diagnostics:await page.evaluate(()=>({open:document.querySelector("#voice").open, status:document.querySelector("#voice-status").textContent, toasts:document.querySelector("#toasts").textContent, recorder:String(window.MediaRecorder).slice(0,100), debug:window.__voiceDebug,opening:!!voiceOpening,recording:!!recording}))})); throw error;
    });
    await page.locator("#voice-cancel").evaluate(button=>button.click());
    await page.waitForFunction(()=>document.querySelector("#voice-record").textContent === "Start recording");
    assert.equal(audioCalls, 1);
    assert.equal(await page.locator("#input").inputValue(), "");
    await page.waitForFunction(()=>!document.querySelector("#voice-record").disabled);
    await page.locator("#voice-record").evaluate(button=>button.click());
    await page.waitForFunction(()=>document.querySelector("#voice-record").textContent === "Stop and transcribe");
    for (const width of [1280,375,320]) {
      await page.setViewportSize({width,height:900});
      assert.equal(await page.locator("#voice").evaluate(el=>el.scrollWidth > el.clientWidth), false);
      await page.screenshot({path:resolve(evidence, `${name}-voice-${width}.png`), animations:"disabled"});
    }
    await page.waitForFunction(()=>!document.querySelector("#voice-record").disabled);
    await page.locator("#voice-record").evaluate(button=>button.click());
    await page.waitForFunction(()=>document.querySelector("#input").value === "fixture voice draft");
    assert.equal(audioCalls, 2);
    assert.ok(await page.evaluate(()=>window.__audioStopped >= 2));
    await page.locator("#input").fill("");
    await page.setViewportSize({width:1280,height:900});
    report.flows.push(`${name}: audio ${name === "chromium" ? "file chooser" : "file input injection"} -> authenticated transcription fixture -> draft only; recorder fixture cancel/stop cleans tracks`);
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    if (name === "chromium") {
      await page.locator('input[name="via"][value="openai"]').check();
      await page.locator("#f-model").fill("gpt-6.1-sol");
      await page.locator("#f-agent-model").fill("separate-agent-model");
      await page.locator("#f-effort").selectOption("high");
      await page.locator("#f-speed").selectOption("priority");
      await page.locator("#f-agent-model").scrollIntoViewIfNeeded();
      await page.screenshot({path: resolve(evidence, "api-settings-1280.png")});
      await page.setViewportSize({width: 375, height: 900});
      await page.locator("#f-speed").scrollIntoViewIfNeeded();
      await page.screenshot({path: resolve(evidence, "api-settings-375.png")});
      await page.setViewportSize({width: 1280, height: 900});
      await page.getByRole("button", {name: "Save and reconnect", exact: true}).click();
      await page.waitForFunction(() => !document.querySelector("#settings").open);
      const configured = await fetch(base + "/api/state", {headers}).then((r) => r.json());
      assert.equal(configured.settings.demo, true, "Backend must remain offline");
      assert.equal(configured.settings.api_provider, "openai");
      assert.equal(configured.settings.agent_model, "separate-agent-model");
      assert.equal(configured.settings.reasoning_effort, "high");
      assert.equal(configured.settings.service_tier, "priority");
      report.flows.push("OpenAI/agent/effort/tier settings saved through UI with offline backend");
      await page.getByRole("button", { name: "Settings", exact: true }).click();
      await page.locator('input[name="via"][value="api"]').check();
      await page.locator("#f-speed").selectOption("fast");
      await page.getByRole("button", {name: "Save and reconnect", exact: true}).click();
      await page.waitForFunction(() => !document.querySelector("#settings").open);
      const anthropic = await fetch(base + "/api/state", {headers}).then((r) => r.json());
      assert.equal(anthropic.settings.api_provider, "anthropic");
      assert.equal(anthropic.settings.api_speed, "fast");
      assert.equal(anthropic.settings.service_tier, "");
      assert.equal(anthropic.settings.model, "");
      assert.equal(anthropic.settings.agent_model, "");
      report.flows.push("API vendor change clears previous model and incompatible speed settings");
      await page.getByRole("button", { name: "Settings", exact: true }).click();
    }
    await page.locator("#imp-since").fill("");
    await page.locator("#imp-files").scrollIntoViewIfNeeded();
    if (name === "chromium") {
      // Abort an in-flight upload through the visible Cancel control.
      await page.route("**/api/import/upload?*", async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 350));
        try { await route.continue(); } catch {}
      });
      await page.locator("#imp-file-input").setInputFiles({name: "cancel.jsonl", mimeType: "application/jsonl", buffer: Buffer.from(data)});
      await page.locator("#imp-cancel").click();
      await page.waitForFunction(() => document.querySelector("#imp-progress").textContent.includes("cancelled"));
      await page.waitForFunction(() => !document.querySelector("#imp-files").disabled);
      await page.unroute("**/api/import/upload?*");
      assert.equal((await fetch(base + "/api/state", {headers}).then((r) => r.json())).status.messages, 0);
      report.flows.push("cancel before import leaves memory unchanged");

      const chooser = page.waitForEvent("filechooser");
      await page.getByRole("button", {name: "Choose files", exact: true}).click();
      await (await chooser).setFiles({name: "session.jsonl", mimeType: "application/jsonl", buffer: Buffer.from(data)});
      await page.waitForFunction(() => document.querySelector("#imp-progress").textContent.includes("Choose sessions"));
      assert.equal(await page.locator("#imp-list input:checked").count(), 1);
      await page.screenshot({path: resolve(evidence, "chromium-preview.png")});
      await page.locator("#imp-go").click();
      await page.waitForFunction(() => document.querySelector("#imp-progress").textContent.startsWith("Imported "));
      const messages = await fetch(base + "/api/messages", {headers}).then((r) => r.json());
      assert.equal(messages.messages.length, 3);
      assert(!JSON.stringify(messages).includes(secret));
      assert(JSON.stringify(messages).includes("[redacted]"));
      report.flows.push("file picker -> preview -> import -> redacted durable messages");

      await page.locator("#imp-file-input").setInputFiles({name: "same-session.jsonl", mimeType: "application/jsonl", buffer: Buffer.from(data)});
      await page.waitForFunction(() => document.querySelector("#imp-progress").textContent.includes("Choose sessions"));
      assert.equal(await page.locator("#imp-list input").count(), 0);
      report.flows.push("same upload produces no duplicate sessions");
      const downloadEvent = page.waitForEvent("download");
      await page.getByRole("button", {name: "Download memory backup", exact: true}).click();
      const download = await downloadEvent;
      assert(download.suggestedFilename().endsWith(".zip"));
      await download.saveAs(resolve(evidence, "browser-memory.zip"));
      report.flows.push("authenticated memory ZIP download");
    }
    for (const width of [1280, 390, 375, 320]) {
      await page.setViewportSize({width, height: 900});
      await page.locator("#imp-files").scrollIntoViewIfNeeded();
      const overflow = await page.evaluate(() => ({
        document: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        sheet: document.querySelector("#settings").scrollWidth > document.querySelector("#settings").clientWidth
      }));
      assert.equal(overflow.document, false, `${name} ${width}: document overflow`);
      assert.equal(overflow.sheet, false, `${name} ${width}: settings overflow`);
      await page.screenshot({path: resolve(evidence, `${name}-${width}.png`)});
      report.layouts.push({browser: name, width, overflow});
    }
    await context.close();
  } finally { await browser.close(); }
}
assert.deepEqual(errors, []);
await writeFile(resolve(evidence, "report.json"), JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify(report));
}
try { await main(); }
finally {
  if (child && child.exitCode === null) {
    const exited = once(child, "exit");
    child.kill("SIGINT");
    await exited;
  }
}
