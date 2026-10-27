---
type: doc
status: active
tags: [elesync, rest-api, api-keys, integrations]
relatedTo: [elesync]
---

# EleSync REST API (v1)

The REST API lets any app, automation or script use your EleSync vault over plain HTTP. You can search and save memories and read your projects. Apps that speak MCP (Claude Desktop, Claude Code, Cursor and others) should keep using the MCP server; the API is for everything else, such as n8n or Zapier flows, shortcuts, scripts and your own tools.

The API is served by the EleSync dashboard, at the same address, under `/api/v1`. On your own computer that is usually:

```text
http://127.0.0.1:7477/api/v1
```

## API keys

Every request needs an API key. This holds even when the dashboard has no password.

1. Open **AI collaborators** in the dashboard and go to **API keys**.
2. Give the key a name (the app that will use it), and choose:
   - **Read only**: the key can search and read.
   - **Read and save**: the key can also save and delete memories.
   - **Project**: the whole vault, or one project. A project key cannot see anything outside that project.
3. Select **Create key** and copy it straight away. EleSync stores only a fingerprint of the key and will not show it again.

Send the key in the `Authorization` header:

```text
Authorization: Bearer esk_...
```

A key works only with the vault it was made in. Revoke it from the same card at any time; apps using it lose access immediately.

## Responses

Every response is JSON in the same envelope.

Success:

```json
{ "ok": true, "data": { }, "meta": { } }
```

Failure:

```json
{ "ok": false, "error": { "code": "read_only_key", "message": "this key is read-only" } }
```

| Status | `error.code` | Meaning |
| --- | --- | --- |
| 400 | `invalid_json`, `invalid_field`, `invalid_parameter` | The request is malformed; the message says what to fix. |
| 401 | `unauthorized` | No key, or the key is wrong or revoked. |
| 403 | `read_only_key` | The key is read-only and the request would change something. |
| 404 | `not_found` | No such memory, project or endpoint, or one the key may not see. |
| 405 | `method_not_allowed` | The endpoint exists but not with this method. |
| 409 | `already_exists` | A project key tried to save text that already exists outside its project. Nothing about that memory is revealed. |
| 413 | `too_large` | The request body is over 1 MiB. |
| 429 | `rate_limited` | More than 120 requests a minute with this key. Wait for `Retry-After` seconds. |

## Endpoints

### `GET /api/v1`

Describes the API and the key you are using.

```bash
curl -H "Authorization: Bearer $ELESYNC_KEY" http://127.0.0.1:7477/api/v1
```

### `GET /api/v1/memories`

Searches memories, or lists the newest when there is no `q`.

| Parameter | Default | Notes |
| --- | --- | --- |
| `q` | none | Search text, up to 500 characters. |
| `limit` | 20 | From 1 to 100. |
| `offset` | 0 | For paging. |

```bash
curl -H "Authorization: Bearer $ELESYNC_KEY" \
  "http://127.0.0.1:7477/api/v1/memories?q=deploy%20schedule&limit=5"
```

Each memory looks like this:

```json
{ "id": "3f2a...", "content": "Deploys go out on Thursdays", "source": "api",
  "type": "note", "tags": ["ops"], "created_at": "2026-10-08T12:00:00+00:00" }
```

### `POST /api/v1/memories`

Saves a memory. Needs a **Read and save** key.

| Field | Required | Notes |
| --- | --- | --- |
| `content` | yes | Up to 100,000 characters. |
| `tags` | no | A list of up to 20 short strings. |
| `type` | no | A short lowercase word, `note` by default. |
| `source` | no | Which tool saved it, `api` by default. |
| `project_id` | no | Also adds it to that project. A project key always saves into its project. |

```bash
curl -X POST -H "Authorization: Bearer $ELESYNC_KEY" -H "Content-Type: application/json" \
  -d '{"content": "Deploys go out on Thursdays", "tags": ["ops"]}' \
  http://127.0.0.1:7477/api/v1/memories
```

The status is `201` for a new memory and `200` when an identical memory already existed (`meta.created` is then `false`).

### `GET /api/v1/memories/{id}`

Returns one memory and the projects it belongs to.

### `DELETE /api/v1/memories/{id}`

Deletes a memory. Needs a **Read and save** key.

### `GET /api/v1/projects`

Lists projects, or only the key's project for a project key.

### `GET /api/v1/projects/{id}`

Returns a project with up to 100 of its memories.

### `GET /api/v1/projects/{id}/context`

Returns the project's context: its knowledge, open tasks, decisions and handoffs. This is what an AI tool needs to continue the project.

## Security notes

- Keys are long random strings. EleSync stores only their SHA-256 hash, in `api_keys.json` inside the vault.
- A browser page on another site cannot use a key against your dashboard: sending the `Authorization` header needs a cross-origin permission that EleSync never grants.
- Every search, save and delete made through the API is written to the vault's audit log under `api:<key name>`, so you can see what each key did.
- A project key can never pull in a memory from outside its project: saving text that already exists elsewhere in the vault answers `409` and links nothing.
- To reach the API from another device, use HTTPS (EleSync's secure link or your own reverse proxy). Never send a key over plain HTTP outside your own computer.
