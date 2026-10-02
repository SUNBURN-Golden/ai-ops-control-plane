"""Execution surfaces, not interchangeable model aliases. No cloud dispatch."""

CATALOG = {
    'codex': {'name': 'Codex CLI', 'executables': ['codex'], 'harness': 'CODEX_CLI',
              'description': '설치된 Codex CLI의 계정과 모델을 사용합니다.',
              'model_required': False, 'model_hint': '비우면 CLI 기본 모델',
              'login_command': 'codex login', 'models_command': 'codex 실행 후 /model',
              'docs_url': 'https://developers.openai.com/codex/cli/'},
    'claude': {'name': 'Claude Code', 'executables': ['claude'], 'harness': 'CLAUDE_CODE',
               'description': '설치된 Claude Code의 계정과 모델을 사용합니다.',
               'model_required': False, 'model_hint': '비우면 CLI 기본 모델',
               'login_command': 'claude 실행 후 /login', 'models_command': 'claude 실행 후 /model',
               'docs_url': 'https://code.claude.com/docs/en/overview'},
    'cursor': {'name': 'Cursor', 'executables': ['agent', 'cursor-agent'], 'harness': 'CURSOR_CLI',
               'description': 'Cursor CLI로 실행합니다. Cursor 안의 Grok 모델은 Grok Build와 별도입니다.',
               'model_required': True, 'model_hint': 'agent models에서 확인한 정확한 ID',
               'login_command': 'agent login', 'models_command': 'agent models',
               'docs_url': 'https://cursor.com/docs/cli/overview'},
    'glm': {'name': 'GLM', 'executables': ['opencode'], 'harness': 'OPENCODE_ZAI_CODING_PLAN',
            'description': '기존 AIOPS와 같은 OpenCode + Z.AI Coding Plan 경로입니다.',
            'model_required': True, 'model_hint': 'zai-coding-plan/glm-…',
            'login_command': 'opencode auth login → Z.AI Coding Plan',
            'models_command': 'opencode models zai-coding-plan',
            'docs_url': 'https://docs.z.ai/devpack/tool/opencode'},
    'grok_build': {'name': 'Grok Build', 'executables': ['grok'], 'harness': 'GROK_BUILD_CLI',
                   'description': '공식 Grok Build CLI의 로컬 실행과 기존 로그인을 사용합니다.',
                   'model_required': False, 'model_hint': '비우면 Grok CLI 기본 모델',
                   'login_command': 'grok login', 'models_command': 'grok models',
                   'docs_url': 'https://docs.x.ai/build/overview'},
    'devin': {'name': 'Devin', 'executables': ['devin'], 'harness': 'DEVIN_LOCAL_CLI',
              'description': '기존 AIOPS와 같은 Devin 로컬 CLI입니다. 클라우드 세션을 만들지 않습니다.',
              'model_required': False, 'model_hint': '비우면 Devin CLI 기본 모델',
              'login_command': 'devin auth login', 'models_command': 'devin models list',
              'docs_url': 'https://docs.devin.ai/cli'},
}


def public_catalog():
    return [dict(id=key, **value) for key, value in CATALOG.items()]
