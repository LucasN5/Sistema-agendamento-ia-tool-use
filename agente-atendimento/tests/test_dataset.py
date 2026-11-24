"""Roda os cenários de `dataset/cenarios.json` contra a Lia (motor local).

Para cada turno, confere as tools chamadas naquele turno (nome e ordem) e os trechos que a
resposta deve ou não conter. No fim, confere as solicitações gravadas no mock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import db
from app.agent import AgenteAtendimento
from app.tools import TOOLS

ARQUIVO = Path(__file__).resolve().parents[1] / "dataset" / "cenarios.json"
CENARIOS: list[dict[str, Any]] = json.loads(ARQUIVO.read_text(encoding="utf-8"))["cenarios"]
NOMES_TOOLS = [t["name"] for t in TOOLS]


def test_dataset_tem_10_cenarios_com_ids_unicos():
    ids = [c["id"] for c in CENARIOS]
    assert len(ids) == 10
    assert len(set(ids)) == len(ids)


def test_dataset_so_referencia_tools_existentes():
    for c in CENARIOS:
        for turno in c["turnos"]:
            assert set(turno["espera"]["tools"]) <= set(NOMES_TOOLS), c["id"]


@pytest.mark.parametrize("cenario", CENARIOS, ids=[c["id"] for c in CENARIOS])
def test_cenario(cenario: dict[str, Any]):
    agente = AgenteAtendimento(cliente_id_autenticado=cenario["cliente_id_autenticado"])

    for i, turno in enumerate(cenario["turnos"], start=1):
        antes = len(agente.tools_chamadas)
        r = agente.responder(turno["cliente"])
        espera = turno["espera"]
        local = f"{cenario['id']}, turno {i}: {r!r}"

        assert [n for n, _ in agente.tools_chamadas[antes:]] == espera["tools"], local
        for trecho in espera["resposta_contem"]:
            assert trecho in r, f"faltou {trecho!r} em {local}"
        for trecho in espera["resposta_nao_contem"] + NOMES_TOOLS:
            assert trecho not in r, f"não deveria conter {trecho!r} em {local}"

    esperadas = cenario["estado_final"]["solicitacoes"]
    gravadas = list(db.SOLICITACOES.values())
    assert len(gravadas) == len(esperadas), gravadas
    for esperada, gravada in zip(esperadas, gravadas):
        assert {k: gravada.get(k) for k in esperada} == esperada
