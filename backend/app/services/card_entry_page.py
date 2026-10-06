"""Public card-entry page. The credential stays in the URL fragment."""

CARD_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="referrer" content="no-referrer" />
  <title>Save a card</title>
  <style>
    body { font-family: Georgia, serif; background: #f6f3ee; color: #1c1915; margin: 0; }
    main { max-width: 28rem; margin: 3rem auto; padding: 1.5rem; background: #fff; border: 1px solid #e4ddd2; }
    h1 { font-size: 1.4rem; font-weight: normal; }
    p { line-height: 1.45; }
    button { background: #1c1915; color: #fff; border: 0; padding: 0.7rem 1rem; font: inherit; cursor: pointer; }
    #card-container { min-height: 3.5rem; margin: 1rem 0; }
    .error { color: #8a2b2b; }
  </style>
</head>
<body>
  <main>
    <h1 id="title">Add a card securely</h1>
    <p id="notice">Submitting this form saves the card on file with the spa. No charge is being made now.</p>
    <p id="status"></p>
    <div id="card-container" hidden></div>
    <button id="save" type="button" hidden>Save card on file</button>
  </main>
  <script>
    const statusNode = document.getElementById("status");
    const saveButton = document.getElementById("save");
    const cardHost = document.getElementById("card-container");
    const token = (location.hash || "").replace(/^#/, "");
    function show(message, isError) {
      statusNode.textContent = message;
      statusNode.className = isError ? "error" : "";
    }
    if (!token) {
      show("This secure link is invalid or has expired.", true);
    } else {
      openSession();
    }
    async function openSession() {
      let payload;
      try {
        const response = await fetch("/api/v1/card-entry/session", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token })
        });
        payload = await response.json();
        if (!response.ok) throw new Error("unavailable");
      } catch (err) {
        show("This secure link is invalid or has expired.", true);
        return;
      }
      if (payload.state === "already_on_file") {
        show("A card is already on file for this appointment.", false);
        return;
      }
      document.getElementById("title").textContent = "Add a card securely for " + (payload.spa_name || "your appointment");
      const sdk = payload.environment === "sandbox"
        ? "https://sandbox.web.squarecdn.com/v1/square.js"
        : "https://web.squarecdn.com/v1/square.js";
      await loadScript(sdk);
      const payments = window.Square.payments(payload.application_id, payload.location_id);
      const card = await payments.card();
      cardHost.hidden = false;
      await card.attach("#card-container");
      saveButton.hidden = false;
      saveButton.addEventListener("click", async () => {
        saveButton.disabled = true;
        try {
          const verificationDetails = {
            intent: "STORE",
            customerInitiated: true,
            sellerKeyedIn: false
          };
          const billingContact = billingContactFrom(payload.billing_contact);
          if (billingContact) verificationDetails.billingContact = billingContact;
          const tokenResult = await card.tokenize(verificationDetails);
          if (tokenResult.status !== "OK" || !tokenResult.token) {
            show("The card could not be saved. You can try again with this link.", true);
            saveButton.disabled = false;
            return;
          }
          const saved = await fetch("/api/v1/card-entry/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              token: token,
              source_id: tokenResult.token
            })
          });
          const body = await saved.json();
          if (body.state === "saved") {
            cardHost.hidden = true;
            saveButton.hidden = true;
            show("Your card was saved securely.", false);
            return;
          }
          if (body.state === "already_on_file") {
            cardHost.hidden = true;
            saveButton.hidden = true;
            show("A card is already on file for this appointment.", false);
            return;
          }
          show(body.detail || "The card could not be saved. You can try again with this link.", true);
          saveButton.disabled = false;
        } catch (err) {
          show("The card could not be saved. You can try again with this link.", true);
          saveButton.disabled = false;
        }
      });
    }
    function billingContactFrom(source) {
      if (!source || typeof source !== "object") return null;
      const contact = {};
      ["givenName", "familyName", "email", "phone"].forEach((key) => {
        if (typeof source[key] === "string" && source[key].trim()) contact[key] = source[key].trim();
      });
      return Object.keys(contact).length ? contact : null;
    }
    function loadScript(src) {
      return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
    }
  </script>
</body>
</html>
"""
