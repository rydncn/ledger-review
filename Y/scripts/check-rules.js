#!/usr/bin/env node
/**
 * Turns the project's source rules into a CI check. Each rule in a JSON config says
 * which folders and file types to scan, a pattern that must not appear, and
 * the message to show. No dependencies.
 *
 *   node check-rules.js [path/to/rules.json] [repo-root]
 *
 * Exits 0 when clean, 1 on any violation, unreadable file, or rule that found
 * no files to check (a check that scans nothing should not pass).
 *
 * Rule fields:
 *   id          short name shown in output
 *   message     what's wrong and where the rule is documented
 *   dirs        folders to scan, relative to the repo root
 *   extensions  file types to scan, e.g. [".ts", ".tsx"]
 *   pattern     regular expression that must not match (source string). It
 *               runs over the whole file, so \s also matches line breaks and
 *               catches calls split across lines by a formatter.
 *   flags       optional regex flags, e.g. "i"
 *   allow       optional regex: a match is ignored when the matched text
 *               also matches this (e.g. "^(Array|Buffer)\\s*\\." for .from)
 *   jsonValues  optional: for .json files, check string values only and
 *               report the key path
 *
 * In JavaScript and TypeScript files, comments are blanked out before
 * matching (strings are left alone). Test files and node_modules are skipped.
 * For a genuine exception, add "rules-check-ignore: <reason>" in a comment
 * on the line where the match starts.
 */
const fs = require("fs");
const path = require("path");

const IGNORE_MARKER = "rules-check-ignore:";
const SKIP_DIRS = new Set([
  "node_modules",
  "__tests__",
  ".git",
  "dist",
  "build",
  ".next",
]);
const JS_LIKE = new Set([".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"]);

const isTestFile = (rel) => /\.(test|spec)\.[cm]?[jt]sx?$/.test(rel);

/**
 * Replace JS/TS comments with spaces, keeping line breaks so line numbers
 * survive, and leaving string and template literals untouched. Regex
 * literals are not parsed; a `//` inside one would be blanked, which can only
 * hide a match, never invent one.
 */
function blankComments(source) {
  let out = "";
  let i = 0;
  let quote = null;
  while (i < source.length) {
    const c = source[i];
    const next = source[i + 1];
    if (quote) {
      out += c;
      // Plain strings cannot span lines, so a stray apostrophe in JSX text
      // ("Don't") never swallows the rest of the file.
      if (c === "\n" && quote !== "`") {
        quote = null;
        i += 1;
        continue;
      }
      if (c === "\\") {
        out += next ?? "";
        i += 2;
        continue;
      }
      if (c === quote) quote = null;
      i += 1;
    } else if (c === "'" && /[A-Za-z0-9]/.test(source[i - 1] ?? "")) {
      // An apostrophe inside a word ("Don't" in JSX text) is not a string.
      out += c;
      i += 1;
    } else if (c === '"' || c === "'" || c === "`") {
      quote = c;
      out += c;
      i += 1;
    } else if (c === "/" && next === "/") {
      while (i < source.length && source[i] !== "\n") {
        out += " ";
        i += 1;
      }
    } else if (c === "/" && next === "*") {
      out += "  ";
      i += 2;
      while (
        i < source.length &&
        !(source[i] === "*" && source[i + 1] === "/")
      ) {
        out += source[i] === "\n" ? "\n" : " ";
        i += 1;
      }
      if (i < source.length) {
        out += "  ";
        i += 2;
      }
    } else {
      out += c;
      i += 1;
    }
  }
  return out;
}

const flagsWithout = (flags, drop) =>
  [...new Set((flags || "").replaceAll(drop, ""))].join("");

function compile(rule) {
  return {
    ...rule,
    regex: new RegExp(rule.pattern, flagsWithout(rule.flags, "g") + "g"),
    allowRegex: rule.allow
      ? new RegExp(rule.allow, flagsWithout(rule.flags, "g"))
      : null,
  };
}

/**
 * True when the line carries the ignore marker in a comment. In JS-like files
 * the marker must disappear once comments are blanked, so a string that
 * happens to contain it does not count.
 */
function hasIgnoreComment(lines, scannedLines, line) {
  if (!lines[line - 1].includes(IGNORE_MARKER)) return false;
  return !scannedLines || !scannedLines[line - 1].includes(IGNORE_MARKER);
}

/** Violations of one rule in one file's source text, one per line at most. */
function checkSource(rule, source, relPath) {
  const jsLike = JS_LIKE.has(path.extname(relPath));
  const scanned = jsLike ? blankComments(source) : source;
  const lines = source.split("\n");
  const scannedLines = jsLike ? scanned.split("\n") : null;
  const seen = new Set();
  const found = [];
  rule.regex.lastIndex = 0;
  for (const m of scanned.matchAll(rule.regex)) {
    if (rule.allowRegex && rule.allowRegex.test(m[0])) continue;
    const line = scanned.slice(0, m.index).split("\n").length;
    if (seen.has(line) || hasIgnoreComment(lines, scannedLines, line)) continue;
    seen.add(line);
    found.push({
      rule: rule.id,
      file: relPath,
      line,
      // Controls shown as \xNN, so the line that broke a rule cannot drive the terminal.
      text: lines[line - 1]
        .trim()
        .replace(
          /[\u0000-\u0008\u000b-\u001f\u007f]/g,
          (c) => `\\x${c.charCodeAt(0).toString(16).padStart(2, "0")}`,
        ),
    });
  }
  return found;
}

/** Violations of one rule in the string values of a parsed JSON file. */
function checkJsonValues(rule, json, relPath) {
  const found = [];
  const walk = (value, keyPath) => {
    if (typeof value === "string") {
      rule.regex.lastIndex = 0;
      if (rule.regex.test(value)) {
        found.push({ rule: rule.id, file: relPath, key: keyPath, text: value });
      }
    } else if (value && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) {
        walk(v, keyPath ? `${keyPath}.${k}` : k);
      }
    }
  };
  walk(json, "");
  return found;
}

function listFiles(root, dir, extensions) {
  const abs = path.join(root, dir);
  if (!fs.existsSync(abs)) return [];
  const out = [];
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const rel = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name))
        out.push(...listFiles(root, rel, extensions));
    } else if (
      extensions.includes(path.extname(entry.name)) &&
      !isTestFile(rel)
    ) {
      out.push(rel);
    }
  }
  return out;
}

/** Returns { violations, errors } for a whole repository. */
function checkRepo(rules, root) {
  const violations = [];
  const errors = [];
  for (const rule of rules.map(compile)) {
    const files = rule.dirs.flatMap((dir) =>
      listFiles(root, dir, rule.extensions),
    );
    if (files.length === 0) {
      errors.push({
        rule: rule.id,
        file: rule.dirs.join(", "),
        message: "no files to check; fix the rule's dirs or extensions",
      });
    }
    {
      for (const rel of files) {
        const text = fs.readFileSync(path.join(root, rel), "utf8");
        if (rule.jsonValues && rel.endsWith(".json")) {
          let json;
          try {
            json = JSON.parse(text);
          } catch (e) {
            errors.push({
              rule: rule.id,
              file: rel,
              message: `invalid JSON: ${e.message}`,
            });
            continue;
          }
          violations.push(...checkJsonValues(rule, json, rel));
        } else {
          violations.push(...checkSource(rule, text, rel));
        }
      }
    }
  }
  return { violations, errors };
}

function main() {
  const configPath = path.resolve(process.argv[2] || "rules.json");
  const root = path.resolve(process.argv[3] || ".");
  const rules = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const messages = Object.fromEntries(rules.map((r) => [r.id, r.message]));
  const { violations, errors } = checkRepo(rules, root);
  for (const e of errors) {
    console.error(`! [${e.rule}] ${e.file}: ${e.message}`);
  }
  for (const v of violations) {
    const where = v.key ? `${v.file} (${v.key})` : `${v.file}:${v.line}`;
    console.error(
      `✗ [${v.rule}] ${where}\n    ${v.text}\n    ${messages[v.rule]}`,
    );
  }
  if (violations.length === 0 && errors.length === 0) {
    console.log(`✓ ${rules.length} rule(s) checked, no violations.`);
    return;
  }
  if (violations.length > 0) {
    console.error(
      `\n${violations.length} violation(s). For a genuine exception, add "${IGNORE_MARKER} <reason>" in a comment on that line.`,
    );
  }
  process.exitCode = 1;
}

if (require.main === module) main();

module.exports = {
  blankComments,
  compile,
  checkSource,
  checkJsonValues,
  checkRepo,
};
