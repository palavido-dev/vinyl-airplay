const path = require("node:path");
const { getDefaultConfig } = require("expo/metro-config");

const projectRoot = __dirname;
const workspaceRoot = path.resolve(projectRoot, "../..");

const config = getDefaultConfig(projectRoot);

// Watch the monorepo so `@vinyl-jukebox/shared` hot-reloads.
config.watchFolders = [workspaceRoot];

// Prefer the app's node_modules, then the workspace root (pnpm).
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, "node_modules"),
  path.resolve(workspaceRoot, "node_modules"),
];

// Keep hierarchical lookup enabled for pnpm's nested .pnpm store.
config.resolver.disableHierarchicalLookup = false;

module.exports = config;
