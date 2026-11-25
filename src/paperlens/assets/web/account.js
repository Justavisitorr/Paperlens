"use strict";
(async () => {
  const $ = id => document.getElementById(id);
  const boot = JSON.parse($("account-boot").textContent);
  let challenge = "", account = null;
  const message = text => { $("message").textContent = text; };
  async function request(url, body) {
    const headers = {"Content-Type":"application/json"};
    if (account) { headers["X-PaperLens-CSRF"] = account.csrf; headers["X-PaperLens-Workspace"] = account.workspace; }
    const reply = await fetch(url, {method:"POST", headers, body:JSON.stringify(body || {})});
    const data = await reply.json();
    if (!reply.ok) throw new Error(data.detail || "暂时无法完成，请稍后重试。");
    return data;
  }
  function download(blob, name) {
    const a = document.createElement("a"), url = URL.createObjectURL(blob);
    a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function action(id, handler) {
    $(id).addEventListener("click", async event => {
      event.preventDefault(); $(id).disabled = true; message("");
      try { await handler(); } catch(error) { message(error.message); }
      finally { $(id).disabled = false; }
    });
  }
  $("github").hidden = !boot.github;
  $("email-form").hidden = !boot.email;
  $("mail-note").textContent = boot.test_mail
    ? "本机验收模式：验证码保存在数据目录的 test-mail 文件夹，不会实际发送邮件。"
    : boot.email ? "验证码 10 分钟内有效。首次登录会创建独立账户。" : "管理员尚未启用邮箱登录。";
  if (boot.page === "account") {
    $("login").hidden = true; $("profile").hidden = false; $("migration").hidden = false;
    $("sign-title").textContent = "绑定另一种登录方式";
    $("github").textContent = "绑定 GitHub 登录";
    await window.paperLensCloudReady;
    account = window.paperLensAccount;
    if (!account) { message("账户资料暂未连接，请刷新后再操作。"); return; }
    $("name").textContent = account.user.name;
    $("identities").textContent = account.identities.map(i => i.provider === "email" ? i.subject : `GitHub · ${i.subject}`).join(" · ");
    const renderSync = () => {
      const current = window.paperLensSync.snapshot();
      $("sync").textContent = current.status;
      $("conflict-section").hidden = !Object.keys(current.conflicts).length;
      $("conflicts").replaceChildren();
      for (const [key, remote] of Object.entries(current.conflicts)) {
        const box = document.createElement("div"); box.className = "conflict";
        const label = document.createElement("strong"); label.textContent = key;
        box.append(label);
        for (const [text, value, useLocal] of [["此设备版本", localStorage.getItem(key), true], ["服务器版本", remote.value, false]]) {
          const pre = document.createElement("pre"); pre.textContent = value === null ? "（已删除）" : value;
          const button = document.createElement("button"); button.textContent = "保留" + text;
          button.onclick = () => window.paperLensSync.resolve(key, useLocal);
          box.append(pre, button);
        }
        $("conflicts").append(box);
      }
    };
    renderSync(); window.addEventListener("paperlens:sync", renderSync);
  }
  $("email-form").addEventListener("submit", async event => {
    event.preventDefault(); $("request").disabled = true;
    try {
      const result = await request("/auth/email/request", {email:$("email").value});
      challenge = result.challenge; $("code-form").hidden = false; $("code").focus();
      message(boot.test_mail ? "测试验证码已写入本机 test-mail 文件夹。" : "验证码已发送，请查收邮箱。");
    } catch (error) { message(error.message); }
    finally { $("request").disabled = false; }
  });
  $("code-form").addEventListener("submit", async event => {
    event.preventDefault();
    try { await request("/auth/email/verify", {challenge, code:$("code").value}); location.href = boot.page === "account" ? "/account" : "/"; }
    catch (error) { message(error.message); }
  });
  action("github", async () => { location.href = (await request("/auth/github/start")).url; });
  action("logout", async () => {
    await window.paperLensSync.flush();
    if (window.paperLensSync.snapshot().pending && !confirm("仍有未同步的修改，将保留在此设备。确认退出登录？")) return;
    await request("/auth/logout"); location.href = "/login";
  });
  action("sync-now", () => window.paperLensSync.flush());
  action("export-browser", () => download(new Blob([JSON.stringify(window.paperLensSync.exportData(), null, 2)], {type:"application/json"}), "paperlens-browser-data.json"));
  $("import-browser").addEventListener("change", async event => {
    try {
      const file = event.target.files[0]; if (!file) return;
      if (file.size > 10 * 1024 * 1024) throw new Error("浏览器资料不能超过 10 MB。");
      window.paperLensSync.importData(JSON.parse(await file.text()));
      message("资料已导入；如有不同版本，请在本页选择，再刷新阅读页。");
    } catch (error) { message(error.message); }
  });
  action("export-workspace", async () => {
    window.paperLensSync.assertNotesSynced();
    await window.paperLensSync.flush();
    if (window.paperLensSync.snapshot().pending) throw new Error("请先处理同步失败或不同版本，再导出整包。");
    const reply = await fetch("/auth/migration/export", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({password:$("passphrase").value})});
    if (!reply.ok) throw new Error((await reply.json()).detail);
    download(await reply.blob(), "paperlens-workspace.plbackup"); message("已下载加密资料包，请把密码分开保存。");
  });
  $("import-workspace").addEventListener("change", async event => {
    try {
      const file = event.target.files[0]; if (!file) return;
      const form = new FormData(); form.append("archive", file); form.append("password", $("passphrase").value);
      message("正在验证并导入资料…");
      const reply = await fetch("/auth/migration/import", {method:"POST", body:form});
      const result = await reply.json(); if (!reply.ok) throw new Error(result.detail);
      message("资料已导入，请回到文献库查看。"); await window.paperLensSync.flush();
    } catch (error) { message(error.message); }
    finally { event.target.value = ""; }
  });
})();
