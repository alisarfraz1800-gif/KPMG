# SAP / ABAP Assistant for Microsoft Teams

An AI agent that answers SAP and ABAP questions inside Teams chats, powered by Claude.

```
Teams user ──► Teams ──► Azure Bot Service ──► POST /api/messages (this app) ──► Claude API
                                                         │                         │
                                                         │      tools ◄────────────┘
                                                         │       • search / read the team's ABAP sources
                                                         │       • web search limited to SAP sites (help.sap.com, …)
                                                         ◄── reply posted back into the chat / thread
```

**What it does**

- Answers in 1:1 chats, and in group chats or channels when @mentioned.
- Remembers each conversation (each channel thread separately). Type `reset` to start over.
- Reads the team's ABAP sources (by default this repository, e.g. `BID_atc_code.txt`)
  to answer questions like *"What does CREATE_DEEP_ENTITY do for GeneralDetailsSet?"*
  and cites file and line numbers.
- Searches SAP documentation (help.sap.com, community.sap.com, …) and lists its sources.
- Shows a typing indicator and answers in the background, so slow answers don't time out in Teams.

| File | Purpose |
|---|---|
| `app.py` | Teams bridge: Microsoft 365 Agents SDK host, message routing, and reply splitting |
| `sap_agent.py` | Claude agent: system prompt, tools, tool loop, and per-conversation history |
| `abap_sources.py` | Read-only search and read access to the ABAP source folder |
| `appPackage/` | Teams app manifest and icons |

## 1. Run locally

```bash
cd teams_agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then set ANTHROPIC_API_KEY
python app.py               # listens on http://localhost:3978/api/messages
```

To try it without Azure, set `CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED=true`
in `.env` and connect the [Microsoft 365 Agents Playground](https://learn.microsoft.com/microsoft-365/agents-sdk/test-with-toolkit-project)
(`npm install -g @microsoft/m365agentsplayground`, then `agentsplayground -e http://localhost:3978/api/messages`).

Run the tests with `pip install pytest && pytest`.

## 2. Connect it to Teams

1. **Create an Azure Bot** (Azure portal → *Create a resource* → *Azure Bot*).
   Choose **Single Tenant**, and let it create a new Microsoft Entra app registration.
2. In the app registration, create a **client secret**. Put the IDs in `.env`:
   ```
   CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID=<Application (client) ID>
   CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET=<secret value>
   CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID=<Directory (tenant) ID>
   ```
   Remove `ANONYMOUS_ALLOWED` when you connect to Azure.
3. **Host the app** at a public HTTPS URL, for example on Azure App Service (Python 3.11+,
   startup command `python app.py`, with the `.env` values set as App Settings).
   For development, a dev tunnel works: `devtunnel host -p 3978 --allow-anonymous`.
4. On the Azure Bot, under **Configuration**, set the messaging endpoint to
   `https://<your-host>/api/messages`. Under **Channels**, add **Microsoft Teams**.
5. **Package the Teams app:** in `appPackage/manifest.json` replace both `${{BOT_ID}}` with
   the client ID, then zip the folder contents:
   `cd appPackage && zip ../sap-assistant.zip manifest.json color.png outline.png`.
6. In Teams, go to *Apps → Manage your apps → Upload an app*. Upload the zip (or have your
   Teams admin publish it to the organisation), then add it to a chat or team.

## 3. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Claude API key |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model used for answers |
| `CLAUDE_EFFORT` | `medium` | `low` is faster and cheaper; `high` is more thorough on hard code questions |
| `ABAP_SOURCE_DIR` | repository root | Folder of ABAP sources the agent may read (`.abap`, `.txt`, CDS, …) |
| `SAP_WEB_SEARCH_DOMAINS` | SAP sites | Domains the agent may search; leave empty to turn web search off |

## Notes for production

- **Conversation memory is in-process.** It is lost on restart and isn't shared between
  instances. Run a single instance, or move `SapAgent._histories` to Redis or Cosmos DB.
- Long conversations are summarised automatically on the server side (compaction), so they
  don't run past the model's context window.
- If Claude declines a request, it is retried once on Anthropic's recommended fallback model
  (`fallbacks: "default"`).
- The agent can only **read** files under `ABAP_SOURCE_DIR`. It can't reach SAP systems.
  To answer live questions (for example, the status of bid X), add a tool in `sap_agent.py`
  that calls your SAP Gateway OData service with a technical user.
- Don't put anything confidential in `ABAP_SOURCE_DIR` that people in the chat shouldn't see.
