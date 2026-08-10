module.exports = {
  apps: [
    {
      name: "resume-api",
      script: "venv/bin/uvicorn",
      args: "app.api.routes:app --host 0.0.0.0 --port 8000",
      interpreter: "none",
      env: {
        NODE_ENV: "production",
      },
    },
    {
      name: "email-watcher",
      script: "venv/bin/python",
      args: "-m app.cli watch --interval 60",
      interpreter: "none",
      restart_delay: 5000,
      env: {
        NODE_ENV: "production",
      },
    },
  ],
};
