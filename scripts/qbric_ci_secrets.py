"""The plan's variable values: masked in GitHub's log and replaced in every text file the runner keeps.

GitHub masks only its own job log; the kept outputs are uploaded as artifacts that anyone who can read the
repository can download, so their text is redacted too. JSON and XML are parsed and only their strings change,
so QBRIC reads the counts exactly as before. Binary files (screenshots, traces, the zipped data inside
Playwright's HTML report) cannot be redacted; the artifacts that hold them are kept for a few days only.
Standard library only.
"""
import json
import pathlib
import xml.etree.ElementTree as ElementTree

REDACTED = "***"
MIN_LENGTH = 4  # a shorter value is no secret, and replacing it would garble every number and word that holds it
PLAIN_TEXT = {".log", ".txt", ".md", ".html", ".htm", ".csv"}
XML_ATTRIBUTES = ("message", "value")  # failure and error messages, property values; never counts or names


def values(plan):
    """Every non-blank line of every variable value in the plan, longest first."""
    lines = {line for row in plan["rows"] for v in (row.get("env") or {}).values() if v
             for line in v.splitlines() if line.strip()}
    return sorted(lines, key=lambda v: (-len(v), v))


def mask(plan, env, out):
    """Hide every variable value in GitHub's log before anything else is printed."""
    if env.get("GITHUB_ACTIONS") != "true":
        return
    for value in values(plan):
        out.write("::add-mask::" + value.replace("%", "%25").replace("\r", "%0D") + "\n")
    out.flush()


class Redactor:

    def __init__(self, secrets):
        self.secrets = [s for s in secrets if len(s) >= MIN_LENGTH]

    def text(self, text):
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def tree(self, folder):
        """Redacts every text file under the folder; symbolic links are never followed."""
        if not self.secrets:
            return
        for path in sorted(pathlib.Path(folder).rglob("*")):
            if path.is_file() and not path.is_symlink():
                self.file(path)

    def file(self, path):
        suffix = path.suffix.lower()
        if suffix == ".json":
            self._json(path)
        elif suffix == ".xml":
            self._xml(path)
        elif suffix in PLAIN_TEXT:
            self._plain(path)

    def _plain(self, path):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        redacted = self.text(text)
        if redacted != text:
            path.write_text(redacted, encoding="utf-8")

    def _json(self, path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._plain(path)  # unreadable as JSON, so nothing reads it as JSON: treat it as text
        redacted = self._walk(data)
        if redacted != data:
            path.write_text(json.dumps(redacted, ensure_ascii=False), encoding="utf-8")

    def _walk(self, node):
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self._walk(n) for n in node]
        if isinstance(node, dict):
            return {self.text(k): self._walk(v) for k, v in node.items()}
        return node

    def _xml(self, path):
        try:
            tree = ElementTree.parse(path)
        except (OSError, ElementTree.ParseError):
            return
        changed = False
        for element in tree.iter():
            for field in ("text", "tail"):
                value = getattr(element, field)
                if value and self.text(value) != value:
                    setattr(element, field, self.text(value))
                    changed = True
            for name in XML_ATTRIBUTES:
                value = element.get(name)
                if value and self.text(value) != value:
                    element.set(name, self.text(value))
                    changed = True
        if changed:
            tree.write(path, encoding="utf-8", xml_declaration=True)
