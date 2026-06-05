// CommonJS config — the repo's frontend assets (ge-strategy.js) are plain
// classic scripts, not ES modules, so package.json has no "type": "module".
module.exports = {
  test: {
    globals: true,
    environment: "node",
    include: ["tests/js/**/*.test.js"],
  },
};
