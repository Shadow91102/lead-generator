// PM2 config for the lead scraper.  Start with:  pm2 start ecosystem.config.js
module.exports = {
  apps: [{
    name: "leadgen",
    script: "app.py",

    // Use the venv's Python. The gmaps scraper re-spawns THIS interpreter
    // (sys.executable), so Playwright must be installed in this same venv.
    interpreter: "./.venv/bin/python",
    cwd: __dirname,

    // MUST stay a single fork instance. Job progress lives in an in-memory
    // dict + background threads, so a 2nd instance (or cluster mode) would
    // make the browser's status polls hit a worker that never saw the job.
    instances: 1,
    exec_mode: "fork",

    autorestart: true,
    max_memory_restart: "1500M",   // Chromium is memory-hungry; restart if it leaks

    env: {
      HOST: "127.0.0.1",   // localhost-only: reachable ONLY through nginx in front
      PORT: "21003",
      WAITRESS: "1"        // production WSGI server (single process, threaded)
    }
  }]
};
