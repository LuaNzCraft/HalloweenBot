# Halloween Bot

Bot de evento para Discord focado em interação social, economia leve e caos temporário controlado durante o Halloween.

O projeto combina eventos públicos, missões, moedas, itens, tabuleiro e disputas rápidas. As regras operacionais ficam centralizadas em YAML, enquanto textos e conteúdos extensos são mantidos em catálogos separados.

## Funcionalidades

- Economia com Moedas Doce, Moedas Travessas, dados e XP Bank.
- Loja em Components V2, separada por moeda, com compras rápidas de 1x, 5x e 10x.
- Inventário e Cestas de Doce com recompensas configuráveis e painel em Components V2.
- Tabuleiro animado com casas de doce, travessura, maldição e recompensas por volta.
- Missões pessoais, globais e de evento em um painel Components V2 com resumo e abas.
- Perfil em Components V2, leaderboard e sistema de Doce ou Travessura entre jogadores.
- Guia `/help` em Components V2, com categorias e navegação compacta.
- Observador em Components V2, com cards para anagramas, imagens fornecidas pelo operador e mensagens arquivadas.
- Eventos públicos como Sinais Corrompidos, Arquivos do Observador, Chuva de Doces, Trem Fantasma e Caça aos Doces.
- Carrossel competitivo com inscrições, espectadores, rodadas e premiação.
- Efeitos temporários com expiração, persistência e recuperação após reinício.
- Localização em português do Brasil e inglês.
- Hot reload seguro da configuração operacional.

## Requisitos

- Python 3.11 ou mais recente.
- Um bot criado no Discord Developer Portal.
- Intents `Server Members` e `Message Content` habilitadas no portal.

As dependências principais são `discord.py`, `aiosqlite`, `PyYAML`, `Pillow` e `NumPy`.

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

## Criando cards de imagem

O repositório inclui `tools/movie_frame_tool.py`, um editor gráfico para criar perguntas visuais do Observador. Ele reenquadra uma imagem em 1000×1000, aplica efeitos como blur, VHS, visão noturna e found footage, gera uma versão revelada opcional e pode atualizar o catálogo automaticamente.

O projeto não distribui cenas de filmes, capas ou outras imagens de terceiros. Use somente material próprio, licenciado, em domínio público ou que você tenha autorização para utilizar. Aplicar blur ou outros efeitos não transfere os direitos da imagem original.

### Iniciando a ferramenta

Instale as dependências normalmente pelo `requirements.txt` e execute o programa a partir da raiz do projeto:

```powershell
python tools/movie_frame_tool.py
```

Também é possível abrir uma imagem diretamente:

```powershell
python tools/movie_frame_tool.py "C:\caminho\para\imagem.png"
```

O Python oficial para Windows normalmente já inclui o Tkinter. Em distribuições Linux, instale o pacote `python3-tk` se o módulo `tkinter` não estiver disponível.

### Fluxo recomendado

1. Abra uma imagem com **Abrir** ou cole uma captura com `Ctrl+V`.
2. Arraste para reposicionar e use a roda do mouse para controlar o zoom.
3. Escolha **Preencher** ou **Imagem inteira** e aplique um preset. Para esconder a resposta, o preset **Pergunta borrada** é um bom ponto de partida.
4. Informe o filme ou tema, respostas alternativas separadas por `;` e a dificuldade.
5. Mantenha **Adicionar ao JSON de perguntas** marcado para registrar a pergunta no catálogo.
6. Marque **Salvar também a versão revelada** para mostrar a imagem limpa depois do acerto.
7. Salve com o botão da interface ou `Ctrl+S`.

Executada da raiz, a ferramenta usa por padrão:

```text
Assets/Observador/Movies/                         imagens geradas
content/halloween/observer_movie_questions.json  catálogo de perguntas
content/halloween/horror_movies.json              sugestões de respostas
```

Cada salvamento pode gerar:

```text
movie_001.png
movie_001_revealed.png
```

E uma entrada correspondente no catálogo com `image_path`, `revealed_image_path`, dificuldade e respostas aceitas. O bot aceita PNG, JPG, JPEG ou WebP dentro de `Assets/Observador/Movies`, com até 8 MB por arquivo.

As imagens dessa pasta são ignoradas pelo Git de propósito. Elas precisam ser copiadas separadamente para o mesmo caminho no ambiente onde o bot será executado. Se você decidir versionar assets próprios ou devidamente licenciados em um fork, ajuste o `.gitignore` conscientemente.

### Atalhos

| Atalho | Ação |
|---|---|
| `Ctrl+V` | Colar imagem da área de transferência |
| `Ctrl+O` | Abrir uma imagem |
| `Ctrl+S` | Salvar imagem e, se habilitado, atualizar o catálogo |
| Arrastar com botão esquerdo | Reposicionar enquadramento |
| Roda do mouse | Ajustar zoom |
| Botão direito ou `Shift` + arrastar | Mover o centro do efeito de luz |

## Configuração no Discord

Depois de adicionar o bot ao servidor, use os comandos administrativos:

- `/setup`: abre um painel privado em Components V2 para consultar e configurar canais, cargos, Carrossel, rankings e sistema.
- `/evento`: painel administrativo para acompanhar, iniciar, pausar e encerrar o evento, alem de iniciar ou cancelar manualmente Observador, Chuva de Doces e Carrossel.
- `/observador arquivo adicionar`: transforma uma mensagem do servidor em uma rodada visual; os jogadores tentam identificar livremente o autor.
- `/observador arquivo listar` e `/observador arquivo status`: administram a pool de mensagens arquivadas.

Os IDs definidos por `/setup` são persistidos no SQLite e têm prioridade sobre `guild_defaults`.

O arquivo versionado mantém canais e cargos específicos de servidor vazios. Configure-os pelo `/setup` depois de instalar o bot. Os IDs de emojis em `config/halloween.yml` são referências de exemplo e devem ser substituídos pelos emojis disponíveis na sua comunidade.

## Configuração operacional

O arquivo `config/halloween.yml` controla economia, tempos, probabilidades, recompensas, pools, eventos, itens e comportamento do tabuleiro.

Após alterar somente o YAML, abra `/setup`, vá até **Sistema** e clique em **Recarregar halloween.yml**.

Uma configuração inválida é rejeitada e a última versão válida continua ativa. Alterações em Python exigem reinicialização do bot.

## Comandos para jogadores

- `/help`: abre o guia do evento em um painel privado com categorias.
- `/perfil`: mostra perfil, recursos e inventário.
- `/loja`: abre a loja ou compra um item.
- `/usar`: utiliza um item do inventário.
- `/cestas`: abre o painel de Cestas de Doce.
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
tools/         Ferramentas opcionais para criação de conteúdo
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
- Canais, cargos e demais IDs operacionais devem ser configurados em cada servidor, não publicados no código.

Antes de uma publicação real, valide permissões, hierarquia de cargos, intents, canais, emojis e comportamento de rate limit em um servidor de teste.
