#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");

const scriptPath = path.resolve(__dirname, "..", "ui_service", "ui.py");

if (!fs.existsSync(scriptPath)) {
  console.error("meshbridge: Missing ui_service/ui.py in package.");
  process.exit(1);
}

const envOverride = process.env.MESHTASTIC_BRIDGE_PYTHON;

function parseCommand(command) {
  if (!command) {
    return null;
  }
  const parts = command.split(/\s+/).filter(Boolean);
  return parts.length ? { cmd: parts[0], args: parts.slice(1) } : null;
}

function canRun(candidate) {
  const check = spawnSync(candidate.cmd, [...candidate.args, "--version"], {
    stdio: "ignore",
  });
  return !check.error && check.status === 0;
}

function resolvePython() {
  const override = parseCommand(envOverride);
  if (override && canRun(override)) {
    return override;
  }

  const candidates = [];
  if (process.platform === "win32") {
    candidates.push({ cmd: "py", args: ["-3"] });
  }
  candidates.push({ cmd: "python", args: [] });
  candidates.push({ cmd: "python3", args: [] });

  for (const candidate of candidates) {
    if (canRun(candidate)) {
      return candidate;
    }
  }

  return null;
}

const args = process.argv.slice(2);
if (args[0] === "update") {
  console.log("meshbridge: updating meshtastic-bridge...");
  const update = spawnSync("npm", ["install", "-g", "meshtastic-bridge"], {
    stdio: "inherit",
  });
  if (update.status === 0) {
    const current = spawnSync("npm", ["list", "-g", "meshtastic-bridge", "--depth=0"], {
      stdio: "inherit",
    });
    process.exit(current.status ?? 0);
  }
  process.exit(update.status ?? 1);
}

const python = resolvePython();
if (!python) {
  console.error(
    "meshbridge: Python 3 not found. Set MESHTASTIC_BRIDGE_PYTHON to a python executable."
  );
  process.exit(1);
}

if (args.length > 0) {
  console.warn("meshbridge: CLI flags are not supported yet (WIP UI only).");
}

const result = spawnSync(python.cmd, [...python.args, scriptPath], {
  stdio: "inherit",
});

if (result.error) {
  console.error(`meshbridge: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 0);
