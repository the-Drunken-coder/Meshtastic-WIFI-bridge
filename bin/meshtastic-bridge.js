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

function readInstalledVersion() {
  const result = runNpm(["list", "-g", "meshtastic-bridge", "--depth=0", "--json"], {
    encoding: "utf8",
  });
  if (result.status !== 0 || !result.stdout) {
    if (result.error) {
      console.warn(`meshbridge: npm list failed (${result.error.message})`);
    }
    return null;
  }
  try {
    const data = JSON.parse(result.stdout);
    return data.dependencies?.["meshtastic-bridge"]?.version || null;
  } catch (err) {
    return null;
  }
}

function readLatestVersion() {
  const result = runNpm(["view", "meshtastic-bridge", "version"], {
    encoding: "utf8",
  });
  if (result.status !== 0 || !result.stdout) {
    if (result.error) {
      console.warn(`meshbridge: npm view failed (${result.error.message})`);
    }
    return null;
  }
  return result.stdout.trim() || null;
}

function resolveNpmCommand() {
  if (process.platform !== "win32") {
    return "npm";
  }
  const candidates = [
    process.env.npm_execpath,
    path.join(process.env.APPDATA || "", "npm", "npm.cmd"),
    path.join(process.env.ProgramFiles || "C:\\Program Files", "nodejs", "npm.cmd"),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return "npm";
}

const args = process.argv.slice(2);
if (args[0] === "update") {
  const npmCmd = resolveNpmCommand();
  const before = readInstalledVersion();
  const latest = readLatestVersion();
  console.log("meshbridge: updating meshtastic-bridge...");
  if (process.platform === "win32") {
    console.log(`meshbridge: using ${npmCmd}`);
  }
  if (latest) {
    console.log(`meshbridge: latest version is ${latest}`);
  } else {
    console.log("meshbridge: could not determine latest version");
  }
  if (before) {
    console.log(`meshbridge: installed version is ${before}`);
  } else {
    console.log("meshbridge: could not determine installed version");
  }
  const update = runNpm(["install", "-g", "meshtastic-bridge"], {
    stdio: "inherit",
  });
  if (update.error) {
    console.error(
      `meshbridge: update failed (${update.error.message}). ` +
        "Ensure Node.js and npm are installed and on your PATH."
    );
    process.exit(1);
  }
  if (update.status !== 0) {
    console.error(
      `meshbridge: update failed (code ${update.status}). ` +
        "Try running: npm install -g meshtastic-bridge"
    );
    process.exit(update.status ?? 1);
  }
  const after = readInstalledVersion();
  if (before && after) {
    if (before === after) {
      console.log(`meshbridge: already up to date (${after})`);
    } else {
      console.log(`meshbridge: updated ${before} -> ${after}`);
    }
  } else if (after) {
    console.log(`meshbridge: installed ${after}`);
  } else {
    console.log("meshbridge: update completed");
  }
  process.exit(0);
}

if (args[0] === "install-deps") {
  const requirements = path.resolve(__dirname, "..", "requirements.txt");
  if (!fs.existsSync(requirements)) {
    console.error("meshbridge: requirements.txt not found in package.");
    process.exit(1);
  }
  const pythonForDeps = resolvePython();
  if (!pythonForDeps) {
    console.error(
      "meshbridge: Python 3 not found. Set MESHTASTIC_BRIDGE_PYTHON to a python executable."
    );
    process.exit(1);
  }
  console.log(`meshbridge: installing Python deps from ${requirements}`);
  const pipInstall = spawnSync(
    pythonForDeps.cmd,
    [...pythonForDeps.args, "-m", "pip", "install", "-r", requirements],
    { stdio: "inherit" }
  );
  if (pipInstall.error) {
    console.error(`meshbridge: pip failed (${pipInstall.error.message})`);
    process.exit(1);
  }
  if (pipInstall.status !== 0) {
    console.error(
      `meshbridge: pip exited with code ${pipInstall.status}. ` +
        "Try running the command manually."
    );
    process.exit(pipInstall.status ?? 1);
  }
  console.log("meshbridge: dependencies installed");
  process.exit(0);
}

function runNpm(args, options = {}) {
  const cli = resolveNpmCli();
  if (!cli) {
    return { status: 1, error: new Error("npm CLI not found") };
  }
  return spawnSync(process.execPath, [cli, ...args], options);
}

function resolveNpmCli() {
  const execPath = process.env.npm_execpath;
  if (execPath && fs.existsSync(execPath) && execPath.includes("npm-cli")) {
    return execPath;
  }
  try {
    const cli = require.resolve("npm/bin/npm-cli.js");
    if (cli && fs.existsSync(cli)) {
      return cli;
    }
  } catch (_) {
    /* ignore */
  }
  try {
    const cli = require.resolve("npm/bin/npm-cli.cjs");
    if (cli && fs.existsSync(cli)) {
      return cli;
    }
  } catch (_) {
    /* ignore */
  }
  // Last resort: guess common install locations
  const guesses = [
    path.join(process.execPath, "..", "node_modules", "npm", "bin", "npm-cli.js"),
    path.join(process.execPath, "..", "node_modules", "npm", "bin", "npm-cli.cjs"),
  ];
  for (const guess of guesses) {
    if (fs.existsSync(guess)) {
      return guess;
    }
  }
  return null;
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
