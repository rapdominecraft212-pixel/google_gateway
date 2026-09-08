(function () {
  "use strict";

  var $ = function (sel) {
    return document.querySelector(sel);
  };
  var $$ = function (sel) {
    return Array.prototype.slice.call(document.querySelectorAll(sel));
  };

  var POLL_MS = 10000;
  var NOMES = { dia: "dia RPD" };
  var ROTULOS_STATUS = {
    ok: "OK",
    alerta: "ALERTA",
    critico: "CRITICO",
    erro: "ERRO",
    esgotada: "ESGOTADA",
    invalida: "INVALIDA",
    desativada: "DESATIVADA",
  };
  var CORES = [
    "var(--color-text-strong)",
    "var(--color-text-weak)",
    "var(--color-text-weaker)",
    "var(--color-icon)",
  ];

  var cache = {
    resumo: null,
    stats: null,
    requisicoes: null,
    uso: null,
    saude: null,
    configuracao: null,
    filtro: "todas",
  };

  function escapar(texto) {
    var div = document.createElement("div");
    div.textContent = String(texto == null ? "" : texto);
    return div.innerHTML;
  }

  function fmtPct(p) {
    if (p === null || p === undefined) return "--";
    return Math.round(p) + "%";
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "--";
    return Math.round(ms) + "ms";
  }

  function fmtDelta(seg) {
    if (seg === null || seg === undefined || isNaN(seg)) return "?";
    seg = Math.max(0, seg);
    var h = Math.floor(seg / 3600);
    var m = Math.floor((seg % 3600) / 60);
    var s = seg % 60;
    if (h) return h + "H " + (m < 10 ? "0" : "") + m + "M";
    if (m) return m + "M " + (s < 10 ? "0" : "") + s + "S";
    return s + "S";
  }

  function fmtHora(iso) {
    if (!iso) return "--";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString("pt-BR");
  }

  async function buscarJSON(url, opcoes) {
    var resp = await fetch(url, opcoes || {});
    if (!resp.ok) throw new Error("HTTP " + resp.status + " em " + url);
    return resp.json();
  }

  async function carregarTudo() {
    try {
      var dados = await Promise.all([
        buscarJSON("/api/resumo"),
        buscarJSON("/api/estatisticas"),
        buscarJSON("/api/requisicoes?limite=100"),
        buscarJSON("/api/uso?limite=600"),
        buscarJSON("/api/saude"),
        buscarJSON("/api/configuracao"),
      ]);
      cache.resumo = dados[0];
      cache.stats = dados[1];
      cache.requisicoes = dados[2];
      cache.uso = dados[3];
      cache.saude = dados[4];
      cache.configuracao = dados[5];
      esconderErro();
      renderizar();
    } catch (erro) {
      mostrarErro("Servidor inacessível: " + erro.message + " — o servidor está rodando? (python servidor.py)");
    }
  }

  function renderizar() {
    renderStats();
    renderChaves();
    renderGrafico();
    renderReqs();
    renderConfig();
    renderRodape();
  }

  function renderStats() {
    if (!cache.stats || !cache.resumo) return;
    var hoje = cache.stats.hoje || {};
    var maior = 0;
    (cache.resumo.chaves || []).forEach(function (c) {
      var p = c.janelas && c.janelas.dia && c.janelas.dia.percent;
      if (p !== null && p !== undefined && p > maior) {
        maior = p;
      }
    });
    var ok = hoje.ok || 0;
    var falhas = Math.max(0, (hoje.total || 0) - ok);
    var media = cache.stats.media_ms_ok ? fmtMs(cache.stats.media_ms_ok) : "--";
    $("#hero-stats").innerHTML =
      '<div class="stat"><div class="stat-numero">' + ok + '</div><div class="stat-rotulo">requisições reais hoje (ok)</div></div>' +
      '<div class="stat"><div class="stat-numero">' + falhas + '</div><div class="stat-rotulo">falhas hoje (429/erros)</div></div>' +
       '<div class="stat"><div class="stat-numero">' + fmtPct(maior) + '</div><div class="stat-rotulo">RPD mais cheio</div></div>' +
      '<div class="stat"><div class="stat-numero">' + media + '</div><div class="stat-rotulo">média por requisição</div></div>';
    if (cache.saude) {
      $("#atualizado-em").textContent = "atualizado em " + fmtHora(cache.saude.hora);
    }
  }

  function renderChaves() {
    if (!cache.resumo) return;
    var itens = cache.resumo.chaves || [];
    $("#lista-chaves").innerHTML = itens.length
      ? itens.map(function (c, i) { return cartaoChave(c, i); }).join("")
      : '<p class="vazio">Nenhuma chave configurada. Adicione a primeira no formulário abaixo.</p>';
  }

  function cartaoChave(c, idx) {
    var janelasHtml = ["dia"].map(function (n) {
        var j = (c.janelas && c.janelas[n]) || {};
        var p = j.percent;
        var cheio = p === null || p === undefined ? 0 : Math.min(100, p);
        var alerta = p !== null && p !== undefined && p >= c.limiar;
        var restante =
          c.dia_limite > 0
            ? '<span class="janela-reset">restam ' + Math.max(0, c.dia_limite - (c.dia_reqs || 0)) + " de " + c.dia_limite + "</span>"
            : "";
        return (
          '<div class="janela">' +
          '<div class="janela-topo">' +
          "<span>" + NOMES[n] + "</span>" +
          '<span class="janela-pct">' + fmtPct(p) + "</span>" +
          restante +
          '<span class="janela-reset" data-reset="' + (j.resetsAt || "") + '">reset em ' + fmtDelta(segundosAte(j.resetsAt)) + "</span>" +
          "</div>" +
          '<div class="barra"><div class="barra-fill' + (alerta ? " alerta" : "") + '" style="width:' + cheio + '%"></div></div>' +
          "</div>"
        );
      })
      .join("");
    var reqs = c.requisicoes_hoje || {};
    var recentes = (c.falhas_10m || 0) > 0
      ? " · 10min " + (c.ok_10m || 0) + " ok / " + (c.falhas_10m || 0) + " falhas"
      : "";
    var meta =
      "em voo " + c.em_voo +
      " · cooldown " + (c.cooldown_restante ? fmtDelta(c.cooldown_restante) : "--") +
      " · hoje " + (reqs.ok || 0) + " ok / " + (reqs.falhas || 0) + " falhas" + recentes;
    var dot = c.status === "invalida" || !c.ativa ? " dot dot-vazia" : " dot";
    var chipClasse = c.ativa ? "chip chip-" + escapar(c.status) : "chip chip-desativada";
    var rotulo = c.ativa ? ROTULOS_STATUS[c.status] || c.status : ROTULOS_STATUS.desativada;
    var erroHtml = "";
    if (c.erro) {
      erroHtml = '<div class="key-erro">' + escapar(c.erro) + "</div>";
    } else if ((c.status === "critico" || c.status === "erro") && (c.falhas_10m || 0) > 0) {
      erroHtml = '<div class="key-erro">instavel: ' + (c.falhas_10m || 0) + " falhas nos ultimos 10min (" + (c.ok_10m || 0) + " ok)</div>";
    }
    var acoes =
      '<div class="key-acoes">' +
      '<button class="btn-mini" data-acao="alternar" data-idx="' + idx + '">' + (c.ativa ? "desativar" : "ativar") + "</button>" +
      '<button class="btn-mini btn-mini-perigo" data-acao="excluir" data-idx="' + idx + '">excluir</button>' +
      "</div>";
    return (
      '<article class="key-card" data-status="' + escapar(c.status) + '" data-ativa="' + (c.ativa ? "true" : "false") + '">' +
      '<header class="key-topo">' +
      '<span class="key-nome">' + escapar(c.nome) + "</span>" +
      '<span class="' + chipClasse + '"><i class="' + dot + '"></i>' + escapar(rotulo) + "</span>" +
      "</header>" +
      '<div class="janelas">' + janelasHtml + "</div>" +
      '<footer class="key-meta">' + escapar(meta) + "</footer>" +
      erroHtml +
      acoes +
      "</article>"
    );
  }

  function segundosAte(iso) {
    if (!iso) return null;
    var fim = new Date(iso);
    if (isNaN(fim.getTime())) return null;
    return Math.max(0, Math.floor((fim.getTime() - Date.now()) / 1000));
  }

  function renderGrafico() {
    if (!cache.uso) return;
    var snaps = cache.uso.snapshots || [];
    var porChave = {};
    snaps.forEach(function (s) {
      (porChave[s.chave] = porChave[s.chave] || []).push(s);
    });
    var series = [];
    Object.keys(porChave).forEach(function (chave) {
      var pontos = porChave[chave]
        .slice(0, 80)
        .reverse()
        .map(function (s) {
          return { p: s.dia_pct, t: s.quando };
        })
        .filter(function (pt) {
          return pt.p !== null && pt.p !== undefined;
        });
      if (pontos.length >= 2) series.push({ chave: chave, pontos: pontos });
    });
    if (!series.length) {
      $("#grafico").innerHTML =
        '<p class="vazio">Sem histórico ainda. O agendador coleta o uso a cada minuto — aguarde ou clique em Atualizar agora.</p>';
      $("#grafico-legenda").innerHTML = "";
      return;
    }
    var W = 640;
    var H = 200;
    var PAD = 10;
    var maxN = Math.max.apply(
      null,
      series.map(function (s) {
        return s.pontos.length;
      })
    );
    var x = function (i) {
      return PAD + (i / Math.max(1, maxN - 1)) * (W - 2 * PAD);
    };
    var y = function (p) {
      return PAD + (1 - p / 100) * (H - 2 * PAD);
    };
    var linhas = series
      .map(function (s, si) {
        var pts = s.pontos
          .map(function (pt, i) {
            return x(i).toFixed(1) + "," + y(pt.p).toFixed(1);
          })
          .join(" ");
        return (
          '<polyline points="' + pts + '" fill="none" stroke-width="1.5" vector-effect="non-scaling-stroke" style="stroke:' +
          CORES[si % CORES.length] + '"></polyline>'
        );
      })
      .join("");
    var primeiro = series[0].pontos;
    var grade =
      '<line x1="' + PAD + '" y1="' + y(0) + '" x2="' + (W - PAD) + '" y2="' + y(0) + '" class="eixo"></line>' +
      '<line x1="' + PAD + '" y1="' + y(50) + '" x2="' + (W - PAD) + '" y2="' + y(50) + '" class="eixo"></line>' +
      '<line x1="' + PAD + '" y1="' + y(100) + '" x2="' + (W - PAD) + '" y2="' + y(100) + '" class="eixo"></line>' +
      '<text x="2" y="' + (y(0) + 4) + '" class="rotulo-eixo">0%</text>' +
      '<text x="2" y="' + (y(50) + 4) + '" class="rotulo-eixo">50%</text>' +
      '<text x="2" y="' + (y(100) - 3) + '" class="rotulo-eixo">100%</text>' +
      '<text x="' + PAD + '" y="' + (H - 2) + '" class="rotulo-eixo">' + escapar(fmtHora(primeiro[0].t)) + "</text>";
    $("#grafico").innerHTML =
      '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="grafico de uso">' + grade + linhas + "</svg>";
    $("#grafico-legenda").innerHTML = series
      .map(function (s, si) {
        return '<span><i style="background:' + CORES[si % CORES.length] + '"></i>' + escapar(s.chave) + "</span>";
      })
      .join("");
  }

  function renderReqs() {
    if (!cache.requisicoes) return;
    var todas = cache.requisicoes.requisicoes || [];
    var linhas = cache.filtro === "falhas" ? todas.filter(function (r) { return !r.ok; }) : todas;
    $("#tbody-req").innerHTML = linhas.length
      ? linhas
          .map(function (r) {
            var chip = r.ok
              ? '<span class="chip chip-ok"><i class="dot"></i>OK</span>'
              : '<span class="chip chip-erro">' + escapar(r.status || "ERRO") + "</span>";
            return (
              "<tr>" +
              "<td>" + escapar(fmtHora(r.quando)) + "</td>" +
              "<td>" + escapar(r.chave) + "</td>" +
              '<td class="modelo">' + escapar(r.modelo || "--") + "</td>" +
              "<td>" + chip + "</td>" +
              "<td>" + fmtMs(r.ms) + "</td>" +
              "<td>" + (r.tokens_in || 0) + " / " + (r.tokens_out || 0) + "</td>" +
              '<td class="erro">' + escapar(r.erro || "") + "</td>" +
              "</tr>"
            );
          })
          .join("")
      : '<tr><td colspan="7" class="vazio">Nenhuma requisição ainda. Aponte o opencode para o gateway e use.</td></tr>';
    $("#req-contagem").textContent = linhas.length + " de " + todas.length + " requisições registradas";
  }

  function renderConfig() {
    if (!cache.configuracao) return;
    var cfg = cache.configuracao;
    var linhas = [
      ["porta do servidor", cfg.porta],
      ["endpoint do gateway", cfg.endpoint_gateway],
      ["coleta de uso", "a cada " + cfg.intervalo_uso_seg + "s"],
      ["limiar de alerta", fmtPct(cfg.limiar_alerta)],
      ["máximo por chave", cfg.max_conc_por_key + " simultâneos"],
      ["espera após 429", cfg.cooldown_429_seg + "s (só quando o Google não informar o tempo)"],
      ["espera máxima", "segura o request até " + cfg.espera_cooldown_max_seg + "s antes de errar"],
      ["token do gateway", cfg.gateway_token_definido ? "definido" : "livre (só localhost)"],
      [
        "limite por chave (RPD)",
        cfg.limites ? "dia " + cfg.limites.requisicoes_dia + " requisições" : "nenhum",
      ],
      ["chaves", (cfg.chaves || []).join(", ") || "nenhuma"],
      ["modelos", (cfg.modelos || []).join(", ") || "nenhum"],
    ];
    $("#config-info").innerHTML = linhas
      .map(function (l) {
        return "<div class='config-item'><dt>" + escapar(l[0]) + "</dt><dd>" + escapar(l[1]) + "</dd></div>";
      })
      .join("");
  }

  function renderRodape() {
    if (cache.configuracao) {
      $("#legal-porta").textContent = "porta " + cache.configuracao.porta;
    }
    if (cache.saude) {
      var d = new Date(cache.saude.hora);
      if (!isNaN(d.getTime())) {
        $("#hora-servidor").textContent = d.toLocaleTimeString("pt-BR");
      }
    }
  }

  function mostrarErro(mensagem) {
    $("#estado-conexao").hidden = false;
    $("#erro-conexao").textContent = mensagem;
  }

  function esconderErro() {
    $("#estado-conexao").hidden = true;
  }

  async function forcarSincronizarModelos(botao) {
    var original = botao.textContent;
    try {
      await buscarJSON("/api/modelos/sincronizar", { method: "POST" });
      botao.textContent = "feito";
      botao.dataset.done = "true";
    } catch (erro) {
      botao.textContent = "sem chave ativa";
    }
    setTimeout(function () {
      botao.textContent = original;
      delete botao.dataset.done;
    }, 1500);
    carregarTudo();
  }

  async function forcarAtualizacao(botao) {
    var original = botao.textContent;
    try {
      await buscarJSON("/api/atualizar", { method: "POST" });
      botao.textContent = "feito";
      botao.dataset.done = "true";
    } catch (erro) {
      botao.textContent = "falhou";
    }
    setTimeout(function () {
      botao.textContent = original;
      delete botao.dataset.done;
    }, 1500);
    carregarTudo();
  }

  async function adicionarChave() {
    var erro = $("#form-erro");
    var nome = $("#nova-nome").value.trim();
    var key = $("#nova-key").value.trim();
    var tipo = $("#nova-tipo").value;
    erro.textContent = "";
    if (!nome || !key) {
      erro.textContent = "preencha nome e key";
      return;
    }
    var botao = $("#btn-adicionar");
    botao.disabled = true;
    try {
      var resp = await fetch("/api/chaves", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nome: nome, key: key, tipo: tipo }),
      });
      if (!resp.ok) {
        var dados = await resp.json().catch(function () { return {}; });
        throw new Error(dados.erro || "HTTP " + resp.status);
      }
      $("#nova-nome").value = "";
      $("#nova-key").value = "";
    } catch (err) {
      erro.textContent = "falhou: " + err.message;
    }
    botao.disabled = false;
    carregarTudo();
  }

  async function alternarChave(idx) {
    if (!cache.resumo) return;
    var c = (cache.resumo.chaves || [])[idx];
    if (!c) return;
    try {
      await buscarJSON("/api/chaves/" + encodeURIComponent(c.nome), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ativa: !c.ativa }),
      });
    } catch (erro) {
      mostrarErro("Falha ao " + (c.ativa ? "desativar" : "ativar") + " " + c.nome + ": " + erro.message);
      return;
    }
    carregarTudo();
  }

  async function excluirChave(idx) {
    if (!cache.resumo) return;
    var c = (cache.resumo.chaves || [])[idx];
    if (!c) return;
    if (!window.confirm("Excluir a chave " + c.nome + "?")) return;
    try {
      await buscarJSON("/api/chaves/" + encodeURIComponent(c.nome), { method: "DELETE" });
    } catch (erro) {
      mostrarErro("Falha ao excluir " + c.nome + ": " + erro.message);
      return;
    }
    carregarTudo();
  }

  function ligarEventos() {
    $("#btn-atualizar").addEventListener("click", function (e) {
      forcarAtualizacao(e.currentTarget);
    });
    $("#btn-atualizar-topo").addEventListener("click", function (e) {
      forcarAtualizacao(e.currentTarget);
    });
    $("#btn-atualizar-mobile").addEventListener("click", function (e) {
      $("#menu-mobile").hidden = true;
      forcarAtualizacao(e.currentTarget);
    });
    $("#btn-sync-modelos").addEventListener("click", function (e) {
      forcarSincronizarModelos(e.currentTarget);
    });
    $("#btn-adicionar").addEventListener("click", adicionarChave);
    $("#nova-nome").addEventListener("keydown", function (e) {
      if (e.key === "Enter") adicionarChave();
    });
    $("#nova-key").addEventListener("keydown", function (e) {
      if (e.key === "Enter") adicionarChave();
    });
    $("#lista-chaves").addEventListener("click", function (e) {
      var botao = e.target.closest("button[data-acao]");
      if (!botao) return;
      var idx = parseInt(botao.dataset.idx, 10);
      if (isNaN(idx)) return;
      if (botao.dataset.acao === "alternar") alternarChave(idx);
      if (botao.dataset.acao === "excluir") excluirChave(idx);
    });
    $("#btn-menu").addEventListener("click", function () {
      var menu = $("#menu-mobile");
      menu.hidden = !menu.hidden;
    });
    $$(".mobile-nav a").forEach(function (link) {
      link.addEventListener("click", function () {
        $("#menu-mobile").hidden = true;
      });
    });
    $$(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        $$(".tab").forEach(function (t) {
          t.classList.remove("ativa");
        });
        tab.classList.add("ativa");
        cache.filtro = tab.dataset.filtro;
        renderReqs();
      });
    });
  }

  setInterval(function () {
    if (!cache.resumo) return;
    $$(".janela-reset").forEach(function (el) {
      var iso = el.dataset.reset;
      if (!iso) return;
      var seg = segundosAte(iso);
      if (seg !== null) el.textContent = "reset em " + fmtDelta(seg);
    });
  }, 1000);

  document.addEventListener("DOMContentLoaded", function () {
    ligarEventos();
    carregarTudo();
    setInterval(carregarTudo, POLL_MS);
  });
})();
