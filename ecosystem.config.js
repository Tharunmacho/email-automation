module.exports = {
  apps: [
    {
      name: "resume-api",
      script: "venv/bin/uvicorn",
      args: "app.api.routes:app --host 0.0.0.0 --port 8005",
      interpreter: "none",
      env: {
        NODE_ENV: "production",
      },
    },
    {
      name: "resume-worker",
      script: "venv/bin/celery",
      args: "-A app.tasks.celery_app worker --loglevel=INFO --pool=solo",
      interpreter: "none",
      env: {
        NODE_ENV: "production",
      },
    },
  ],
};
