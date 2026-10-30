# dm-awb

One Selenium workflow, run for a saved list of resources.

```bash
source .venv/bin/activate
cp .env.example .env
python server.py
```

Open http://127.0.0.1:8765 to manage the resource list, pick an entry month, run the export for all resources, read the history, and download the PDFs.

## Docker

The container runs the app plus a sign-in screen (a virtual display the user opens in a browser tab). Exports stay hidden. Nothing you type on the sign-in screen is stored by the app — only the saved Power BI session is kept.

```bash
cp .env.example .env
docker compose up -d --build
```

Open http://127.0.0.1:8080. When the session is logged out, **Sign in to Power BI** opens the sign-in screen. Finish Microsoft sign-in there (including any phone prompt). The app notices on its own and closes the screen.

On a remote host, only port 8080 is published. The sign-in screen is served at `/vnc/` on that same address. Exported files, the saved sign-in, the signature, and run history live in the `cra-data` volume.

The resource list is stored in `config.json` and survives restarts. Add, edit, or remove names in the UI; duplicates are rejected. Each resource has an **exclude** flag: excluded resources stay in the list but are skipped on runs. **Run** executes the workflow sequentially for each active resource — one failure does not stop the rest — and progress shows which resource is running.

Each resource run gets its own run folder with a monthly PDF and a merged weekly PDF, linked to its entry in the history.

`python main.py` still runs the export for one resource from the terminal.

Edit the browser steps in `workflow.py`. `main.py` starts Chrome, passes secrets, runs one or all resources, and records each run.

Chrome keeps the signed-in session in `chrome-profile/`. The first run is the manual sign-in. Later runs reuse that profile until the session expires. Only one run can use it at a time.

Session secrets such as `PASSWORD` come from `.env`, or from the `secrets` argument for that run. Each run is appended to `runs.jsonl`. Secret values are not stored there.
