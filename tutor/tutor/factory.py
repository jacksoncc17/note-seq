"""Building a lesson for one piece.

Extracted from the CLI so the HTTP server can construct a *new* session when the
client switches pieces (the server cannot import cli -- cli imports it), and so
"what a session consists of" is stated in one place.

Per-piece: the fact model, the tool box, the session/log, the grounding evidence
and the conductor. Shared across pieces: the ASR, TTS and LLM clients, which is
what makes switching cheap -- a new bank is a few JSON loads, not a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import TutorConfig
from .conductor import Conductor
from .core.facts import FactStore
from .core.tools import ToolBox
from .grounding import Evidence, GroundingVerifier
from .session import Session


def make_llm(cfg: TutorConfig):
  if cfg.llm == 'openai':
    from .llm.openai_compat import OpenAiCompatLlm
    return OpenAiCompatLlm(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key,
                           cfg.llm_timeout_s)
  from .llm.stub import ScriptedTutor
  return ScriptedTutor()


def make_tts(cfg: TutorConfig, play: Optional[bool] = None):
  if cfg.tts == 'openai':
    from .audio.tts import OpenAiCompatTts
    return OpenAiCompatTts(cfg.tts_base_url, cfg.tts_model, cfg.tts_voice,
                           out_dir=cfg.render_dir,
                           play=cfg.tts_play if play is None else play,
                           lang_code=cfg.tts_lang_code)
  from .audio.tts import StubTts
  return StubTts()


@dataclass
class PieceSession:
  cfg: TutorConfig
  facts: FactStore
  session: Session
  tools: ToolBox
  verifier: GroundingVerifier
  evidence: Evidence
  conductor: Conductor
  llm: object
  tts: object
  reason: str = ''            # why this piece was chosen

  @property
  def slug(self) -> str:
    return self.cfg.piece

  def as_tuple(self):
    """The shape older call sites expect."""
    return (self.conductor, self.facts, self.session, self.tools, self.verifier)


def build_piece_session(cfg: TutorConfig, mode: str = 'converse',
                        asr=None, tts=None, llm=None,
                        piece: Optional[str] = None, announce: bool = False,
                        auto_advance: bool = True) -> PieceSession:
  """Load a piece's bank and assemble everything that depends on it.

  Raises SystemExit (from pieces.resolve) when the requested piece does not
  exist, so callers can turn that into a clear message or a 404.
  """
  from .pieces import resolve

  if piece:
    cfg.piece = piece
  cfg.fingerprint, cfg.piece, reason = resolve(cfg)
  if announce:
    print('piece: %s  (%s)' % (cfg.piece, reason))

  facts = FactStore(cfg.fingerprint)
  session = Session(log_path=cfg.log_path,
                    piece=facts.raw.get('source_file', ''))
  tools = ToolBox(facts, session, tier_max=cfg.tier_max,
                  render_dir=cfg.render_dir)
  evidence = Evidence(facts)
  verifier = GroundingVerifier(evidence, facts, policy=cfg.grounding_policy)
  llm = llm if llm is not None else make_llm(cfg)
  tts = tts if tts is not None else make_tts(cfg)
  conductor = Conductor(cfg, facts, tools, session, llm, tts, verifier,
                        mode=mode, auto_advance=auto_advance)
  return PieceSession(cfg=cfg, facts=facts, session=session, tools=tools,
                      verifier=verifier, evidence=evidence,
                      conductor=conductor, llm=llm, tts=tts, reason=reason)
