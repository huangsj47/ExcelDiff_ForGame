"""Knowledge descriptions shared by management UI and the model's index.

Optional single-line frontmatter description is authoritative; old Markdown
falls back to its first heading. Bodies and machine declarations stay intact.
"""

import re

from services.ai.skill_contract import DESCRIPTION_MAX_CHARS, SkillContractError, parse_frontmatter


def document_description(text: str) -> str:
    body = text
    if text.startswith("---\n") or text.startswith("---\r\n"):
        fields, body = parse_frontmatter(text)
        description = fields.get("description", "")
        if len(description) > DESCRIPTION_MAX_CHARS or "<" in description or ">" in description:
            raise SkillContractError("description 过长或含尖括号")
        if description:
            return description
    for line in body.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if match:
            return match[1][:DESCRIPTION_MAX_CHARS]
    return ""


def with_description(text: str, description: str | None) -> str:
    """Update only description, preserving other declarations byte for byte."""
    if description is None:
        document_description(text)
        return text
    if not isinstance(description, str) or "\n" in description or "\r" in description:
        raise SkillContractError("description 必须是一行简介")
    description = description.strip()
    if description:
        document_description(f"---\ndescription: {description}\n---\n")
    if text.startswith("---\n"):
        parse_frontmatter(text)
        end = text.index("\n---", 3)
        lines = [line for line in text[4:end].splitlines() if not re.match(r"^description\s*:", line)]
        if description:
            lines.append(f"description: {description}")
        # Keep unknown keys such as generated_prefixes and the original body.
        return "---\n" + "\n".join(lines) + text[end:]
    return f"---\ndescription: {description}\n---\n\n{text}" if description else text
