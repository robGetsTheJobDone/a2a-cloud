from __future__ import annotations

import os
from pathlib import Path

from code_editor_agent import make_code_editor_agent_class, make_shared_code_editor_agent_class


if os.environ.get("A2A_CODE_EDITOR_MODE", "shared") == "local":
    CodeEditorAgent = make_code_editor_agent_class(
        name=os.environ.get("A2A_CODE_EDITOR_AGENT_NAME", "code-editor-agent"),
        target_name=os.environ.get("A2A_CODE_EDITOR_TARGET_NAME", "workspace"),
        target_path=os.environ.get("A2A_CODE_EDITOR_TARGET_PATH", str(Path.cwd())),
    )
else:
    CodeEditorAgent = make_shared_code_editor_agent_class(
        name=os.environ.get("A2A_CODE_EDITOR_AGENT_NAME", "code-editor-agent"),
    )
