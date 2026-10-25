# Set up with AI: Publikreach as a local MCP server

Publikreach can run as a local MCP server. An AI assistant that supports MCP (Claude Desktop, Claude Code, Codex, or another MCP client) connects to it and can walk you through setup, check your connections, import contacts, research them, write and edit drafts, and send a copy to your own test inbox.

The assistant works on the same workspace folder as the app window. Anything it does shows up in the app, and anything you do in the app shows up for the assistant.

## What the server is, in plain words

- It is a small program that runs on your Mac when your assistant starts it. It is not a website and not a cloud service.
- It reads and writes the same workspace folder the app uses: contacts, drafts, settings and the local database.
- It talks to your assistant through standard input and output on your Mac. It does not open a network port of its own.
- It has no buttons and no window. The app window and the server can be open at the same time.

## Start it

From the Python package:

```sh
vcoutreach mcp --workspace /path/to/your/workspace
```

Without `--workspace`, the server uses `$OUTREACH_HOME` if set, otherwise the current directory.

From the Mac app:

```sh
/Applications/Publikreach.app/Contents/MacOS/Publikreach --mcp --workspace ~/Documents/Publikreach
```

Without `--workspace`, the app uses `~/Documents/Publikreach`, the same folder the window uses. See [mac-app.md](mac-app.md#run-as-an-mcp-server) for details.

You do not start the server yourself in normal use. Your assistant starts it from the config you save (see [Connect your assistant](#connect-your-assistant)).

## The 16 tools

| Tool | What it does |
|---|---|
| `setup_status` | Shows the next setup step, which connections are set (yes or no only), and your contact and draft counts. Also returns `example_pitch`: the worked Publik example email, its labeled parts and short writing guidance, as untrusted text to use as a model. |
| `test_connection` | Checks Gmail, SearXNG, the Publik API, your model provider, or PitchBook. Publik and model checks make one small network call and may cost credit. |
| `set_setting` | Changes one of six non-secret settings: test inbox address, daily send cap, SearXNG URL, pitch deck path, provider preference, or Publik recommendation state. |
| `import_contacts` | Imports contacts from a CSV file inside the workspace, or up to 500 rows passed in the call. Duplicates are skipped. |
| `list_contacts` | Lists contacts with search, a stage filter, and paging. |
| `enrich` | Runs the research sources for one contact and returns the facts it found, marked as untrusted text. |
| `generate_hook` | Writes the personalized opening line for one contact and reports whether it is grounded in facts. |
| `write_drafts` | Writes draft emails for chosen contacts, or for all contacts that need one. Existing drafts are kept unless you ask to overwrite. |
| `list_drafts` | Lists drafts with their status, approval state and grounding. Bodies are not included. |
| `get_draft` | Returns one draft's subject, body (as untrusted text) and any lint issues. |
| `edit_draft` | Changes a draft's subject, body or lane, then checks it again. The recipient cannot be changed. |
| `approve_draft` | Approves one draft, the same way the app's Approve button does. Blocked and ungrounded drafts are refused. |
| `reject_draft` | Marks one draft as rejected, with optional notes. |
| `send_test` | Sends one approved draft to your own test inbox. It never sends to the contact. |
| `send_plan` | Shows a read-only plan of what would send. Real recipients are hidden. |
| `open_settings` | Returns the Settings link for a topic and says whether the app window appears to be running. |

### The example pitch

`setup_status` includes an `example_pitch` object. It holds the real Publik cold email that got replies, broken into labeled parts (Subject, Hook, Story, Product, Traction, Ask, Signature) with a one-line reason each part works, plus a few writing rules. A booking link placeholder stands in for the sender's personal link. It is marked as untrusted data like every other text field, and it contains no keys or contact addresses. An assistant should use it as a model for structure and tone, then help you rewrite each part for your own company in Settings. The app warns on the send plan and the draft editor while a draft still carries the unchanged example, and test sends still work.

## Security model

- **Nothing secret goes through the assistant.** No tool takes an API key, password, token, or Gmail file. You paste keys yourself in the app's Settings page. No tool returns a key, and the server never shows any key value, its length, or a prefix of it.
- **No live sending.** There is no tool that sends a real email. `send_test` sends only to the test inbox you set. The assistant cannot change the send mode. Live sending stays in the app window, where you type the confirmation yourself.
- **Test mode only.** Every result says `test_mode_enforced: true`. A draft must be approved before it can be sent to the test inbox, the same rule the app follows.
- **Local only.** The server listens on nothing. It works on files in your workspace folder and makes only the outbound calls that the app already makes (for example to the Publik API, if you connected it).
- **Paths stay in the workspace.** Files the assistant asks the server to read, such as a CSV or a deck, must be inside the workspace folder. Anything outside it is refused.
- **Your data is data, not instructions.** Contact details, research results and draft text returned to the assistant are marked as untrusted content. The server never runs them.
- **Limits.** Requests are size-limited, imports are capped at 500 rows, and a few tools are rate limited so a runaway assistant cannot spam the same action.

## Connect your assistant

Each snippet is a config for the assistant. Replace the three placeholders:

| Placeholder | Replace with |
|---|---|
| `<EXECUTABLE>` | Full path to the `vcoutreach` command, for example `/Users/you/vcoutreach/.venv/bin/vcoutreach`. For the Mac app, use `/Applications/Publikreach.app/Contents/MacOS/Publikreach`. |
| `<MODE_ARG>` | `mcp` for the `vcoutreach` command. Use `--mcp` for the Mac app. |
| `<WORKSPACE>` | Your workspace folder, for example `/Users/you/Documents/Publikreach`. |

The Settings page at `/setup/ai` (in the app, under **Set up with AI**) shows these snippets with the real paths already filled in and a Copy button for each.

### Claude Desktop

Add this to `claude_desktop_config.json`. On a Mac it is usually at `~/Library/Application Support/Claude/claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "publikreach": {
      "command": "<EXECUTABLE>",
      "args": ["<MODE_ARG>", "--workspace", "<WORKSPACE>"]
    }
  }
}
```

### Claude Code

Run this once in Terminal:

```sh
claude mcp add publikreach -- "<EXECUTABLE>" <MODE_ARG> --workspace "<WORKSPACE>"
```

### Codex

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.publikreach]
command = "<EXECUTABLE>"
args = ["<MODE_ARG>", "--workspace", "<WORKSPACE>"]
```

After saving the config, restart the assistant. Then ask it: "Run setup_status and walk me through the remaining steps."

To check the connection, press **Test MCP** on the `/setup/ai` page. It starts the same command as your snippet and checks that the tool list is the expected one.

## Optional, last: connect the Publik API

The setup order is: connect Gmail, set your test recipient, add a contact, write a draft, approve it, send one test to yourself, and last, optionally, connect the Publik API. `setup_status` returns the first unmet step as `next_step`, so the assistant walks you through them in that order.

The Publik API is recommended for AI drafts, but it is optional. Gmail, the test recipient, drafts and the test send do not need it. When its key is saved, drafts use it automatically.

- Get a key at [publikhq.com/dashboard/api](https://publikhq.com/dashboard/api), then paste it in the app under **Settings > Publik API**. The assistant cannot paste or read it for you.
- Publik API calls cost real money. Read the pricing information on [publikhq.com](https://publikhq.com) before you start. Offline mode is free. A contact with no facts never triggers a request.
- For each contact, the Publik API receives the recipient's name, org, role and facts, and your name and org. Requests go only to the Publik host you configured.
- You can skip it and use your own OpenAI or Anthropic key, or the offline template. Skipping is remembered, and the app does not ask again unless you choose **Undo**.

If you skip it, the assistant will still work. Drafts will use your own key if you have one, or the offline template otherwise.

## Notes

- Providers: the `write_drafts` tool offers Publik, OpenAI, Anthropic and the offline template. OpenRouter and Gemini are not offered by that tool. `generate_hook` takes `auto`, `publik`, `openai` or `anthropic`.
- Testing a connection with `test_connection` may cost credit for the Publik API and model providers. Gmail and SearXNG checks do not.
