const form = document.querySelector("#login-form");
const error = document.querySelector("#login-error");

fetch("/api/session").then((r) => r.json()).then((state) => {
  if (state.admin) location.replace("/");
  if (state.setup_required) document.querySelector("#setup-note").hidden = false;
}).catch(() => {});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const button = form.querySelector("button");
  button.disabled = true;
  error.hidden = true;
  try {
    const response = await fetch("/api/session/login", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.fromEntries(new FormData(form).entries())),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error === "invalid name or password" ? "用户名或密码错误" :
        body.error?.includes("locked") ? "登录失败次数过多，账号已临时锁定，请 15 分钟后再试" : (body.error || "登录失败"));
    }
    location.replace("/");
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
    form.elements.namedItem("password").select();
  } finally { button.disabled = false; }
});
