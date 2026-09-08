# Halloween Bot

Bot de evento para Discord focado em interação social, economia leve e caos temporário controlado durante o Halloween.

O projeto combina eventos públicos, missões, moedas, itens, tabuleiro e disputas rápidas. As regras operacionais ficam centralizadas em YAML, enquanto textos e conteúdos extensos são mantidos em catálogos separados.

## Funcionalidades

- Economia com Moedas Doce, Moedas Travessas, dados e XP Bank.
- Inventário, loja e Cestas de Doce com recompensas configuráveis.
- Tabuleiro animado com casas de doce, travessura, maldição e recompensas por volta.
- Missões pessoais, globais e de evento.
- Perfil, leaderboard e sistema de Doce ou Travessura entre jogadores.
- Eventos públicos como Ghostface, Chuva de Doces, Trem Fantasma e Doces no Chat.
- Carrossel competitivo com inscrições, espectadores, rodadas e premiação.
- Efeitos temporários com expiração, persistência e recuperação após reinício.
- Localização em português do Brasil e inglês.
- Hot reload seguro da configuração operacional.

## Requisitos

- Python 3.11 ou mais recente.
- Um bot criado no Discord Developer Portal.
- Intents `Server Members` e `Message Content` habilitadas no portal.

As dependências principais são `discord.py`, `aiosqlite`, `PyYAML` e `Pillow`.

## Instalação

No PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Crie um arquivo `.env` na raiz do projeto:

```env
DISCORD_TOKEN=seu_token_do_bot
```

O `.env` não é versionado. Nunca grave o token no código ou em `config/halloween.yml`.

Inicie o bot:

```powershell
python main.py
```

## Configuração no Discord

Depois de adicionar o bot ao servidor, use os comandos administrativos:

- `/setup configurar`: define canais e cargos principais.
- `/setup julgamento`: cria ou atualiza a estrutura do Carrossel.
- `/setup ver`: mostra a configuração atual da guild.
- `/evento`: inicia, pausa ou encerra o evento.
- `/leaderboard`: publica ou atualiza os rankings.

Os IDs definidos por `/setup` são persistidos no SQLite e têm prioridade sobre `guild_defaults`.

## Configuração operacional

O arquivo `config/halloween.yml` controla economia, tempos, probabilidades, recompensas, pools, eventos, itens e comportamento do tabuleiro.

Após alterar somente o YAML, use:

```text
/setup recarregar_config
```

Uma configuração inválida é rejeitada e a última versão válida continua ativa. Alterações em Python exigem reinicialização do bot.

## Comandos para jogadores

- `/help`: apresenta o funcionamento do evento.
- `/perfil`: mostra perfil, recursos e inventário.
- `/loja`: abre a loja ou compra um item.
- `/usar`: utiliza um item do inventário.
- `/abrir_cestas`: abre o painel de Cestas de Doce.
- `/tabuleiro`: mostra o tabuleiro atual.
- `/missao`: mostra missões e progresso.
- `/tempo`: informa o próximo evento público.
- `/lang`: altera o idioma das mensagens.

## Estrutura

```text
cogs/          Integração com comandos, listeners e tasks do Discord
services/      Regras de negócio, runtimes, textos e configuração
repositories/  Persistência SQLite por domínio
views/         Componentes interativos do Discord
utils/         Utilitários compartilhados
config/        Configuração operacional central
content/       Perguntas, missões e traduções
Assets/        Recursos visuais de origem
tests/         Testes unitários e de regressão
```

O banco operacional é criado em `data/halloween.sqlite3`. Esse arquivo, backups, caches e artefatos derivados não são enviados ao Git.

## Testes

Execute a suíte completa:

```powershell
python -m unittest discover -s tests -v
```

Verifique também a sintaxe e os catálogos de tradução:

```powershell
python -m compileall -q cogs services repositories utils tests
python -m json.tool content/i18n/pt-BR.json NUL
python -m json.tool content/i18n/en-US.json NUL
```

Os testes usam bancos SQLite temporários e mocks para Discord; eles não devem acessar o banco operacional.

## Segurança e persistência

- Prêmios e missões usam liquidação idempotente para evitar pagamentos duplicados.
- Transferências e fechamentos econômicos são protegidos por transações SQLite.
- Efeitos temporários e eventos pendentes são reconciliados após reinício.
- Bots, mensagens privadas e contextos fora da guild são ignorados quando necessário.
- Tokens, bancos reais e arquivos de fechamento permanecem fora do repositório.

Antes de uma publicação real, valide permissões, hierarquia de cargos, intents, canais, emojis e comportamento de rate limit em um servidor de teste.
