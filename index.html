<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"
  />
  <title>Un pequeño reto</title>
  <style>
    * {
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
      user-select: none;
    }

    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      font-family: Georgia, serif;
      background: #111;
      color: #f4eadb;
    }

    body {
      display: flex;
      justify-content: center;
      align-items: center;
    }

    #app {
      width: 100%;
      height: 100%;
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(to top, #2f251e 0%, #1b1613 35%, #101010 100%);
    }

    .screen {
      position: absolute;
      inset: 0;
      display: none;
    }

    .screen.active {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }

    #startScreen {
      padding: 28px;
      text-align: center;
      background:
        radial-gradient(circle at top, rgba(255, 215, 170, 0.08), transparent 35%),
        linear-gradient(to bottom, #15110e, #0d0d0d);
    }

    #startScreen h1 {
      margin: 0 0 14px;
      font-size: 2rem;
      letter-spacing: 0.5px;
      color: #f3dfc1;
    }

    #startScreen p {
      margin: 0 0 24px;
      max-width: 320px;
      line-height: 1.5;
      color: #d8c3a4;
      font-size: 1rem;
    }

    .btn {
      border: none;
      border-radius: 999px;
      padding: 14px 28px;
      font-size: 1rem;
      font-family: inherit;
      background: linear-gradient(135deg, #c49a6c, #8f6743);
      color: #fffaf3;
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
      cursor: pointer;
      position: relative;
      z-index: 20;
    }

    #gameScreen {
      background:
        linear-gradient(to top, rgba(0,0,0,0.18), rgba(0,0,0,0.04)),
        linear-gradient(to top, #3a2d24 0%, #241c18 40%, #171311 100%);
      justify-content: flex-start;
    }

    #hud {
      width: 100%;
      display: flex;
      justify-content: space-between;
      padding: 14px 16px 0;
      color: #f0dcc0;
      font-size: 0.95rem;
      z-index: 3;
    }

    #hint {
      margin-top: 8px;
      color: #d9c1a0;
      font-size: 0.95rem;
      opacity: 0.95;
      text-align: center;
      padding: 0 18px;
      z-index: 3;
    }

    #gameCanvas {
      width: 100%;
      height: calc(100% - 70px);
      display: block;
      touch-action: none;
    }

    #overlayMessage {
      position: absolute;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      background: rgba(10, 10, 10, 0.42);
      z-index: 50;
      text-align: center;
      padding: 24px;
      pointer-events: auto;
    }

    #overlayMessage .box {
      background: rgba(25, 19, 16, 0.96);
      border: 1px solid rgba(230, 204, 172, 0.18);
      padding: 22px 18px;
      border-radius: 18px;
      max-width: 320px;
      box-shadow: 0 18px 50px rgba(0,0,0,0.35);
      position: relative;
      z-index: 60;
    }

    #overlayMessage h2 {
      margin: 0 0 10px;
      color: #f1dec3;
    }

    #overlayMessage p {
      margin: 0 0 16px;
      color: #dbc6a8;
      line-height: 1.5;
    }

    #letterScreen {
      display: none;
      overflow-y: auto;
      padding: 26px 18px 36px;
      background:
        radial-gradient(circle at top, rgba(255, 220, 170, 0.10), transparent 30%),
        linear-gradient(to bottom, #2f241d, #191311);
    }

    #letterScreen.active {
      display: block;
      animation: fadeIn 1.4s ease;
    }

    .paper {
      width: min(100%, 700px);
      margin: 0 auto;
      background:
        linear-gradient(rgba(255,255,255,0.03), rgba(255,255,255,0.015)),
        #e4cfad;
      color: #4a2e1f;
      border-radius: 18px;
      padding: 28px 22px 34px;
      box-shadow:
        0 18px 50px rgba(0,0,0,0.42),
        inset 0 0 60px rgba(120, 76, 42, 0.12);
      border: 1px solid rgba(95, 58, 33, 0.15);
      position: relative;
      overflow: hidden;
    }

    .paper::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background:
        radial-gradient(circle at 10% 10%, rgba(130, 84, 51, 0.10), transparent 18%),
        radial-gradient(circle at 85% 15%, rgba(130, 84, 51, 0.08), transparent 18%),
        radial-gradient(circle at 50% 100%, rgba(130, 84, 51, 0.08), transparent 20%);
      opacity: 0.8;
    }

    .paper h2 {
      position: relative;
      margin: 0 0 18px;
      text-align: center;
      font-size: 2rem;
      font-weight: normal;
      color: #5c3421;
    }

    .paper p {
      position: relative;
      margin: 0 0 16px;
      font-size: 1.15rem;
      line-height: 1.85;
      white-space: pre-line;
    }

    .signature {
      margin-top: 24px;
      text-align: right;
      font-style: italic;
      color: #6a3d28;
    }

    .typeLine {
      opacity: 0;
      transform: translateY(12px);
      animation: revealLine 0.9s ease forwards;
    }

    @keyframes revealLine {
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @keyframes fadeIn {
      from { opacity: 0; }
      to   { opacity: 1; }
    }
  </style>
</head>
<body>
  <div id="app">
    <section id="startScreen" class="screen active">
      <h1>¿Jugamos?</h1>
      <p>Solo toca la pantalla para saltar y llegar al final.</p>
      <button id="startBtn" class="btn">Empezar</button>
    </section>

    <section id="gameScreen" class="screen">
      <div id="hud">
        <div id="scoreLabel">Puntos: 0</div>
        <div id="goalLabel">Meta: 20</div>
      </div>
      <div id="hint">Toca cualquier parte de la pantalla para saltar</div>
      <canvas id="gameCanvas"></canvas>

      <div id="overlayMessage">
        <div class="box">
          <h2 id="overlayTitle">Uy...</h2>
          <p id="overlayText">Inténtalo otra vez.</p>
          <button id="overlayBtn" class="btn" type="button">Reintentar</button>
        </div>
      </div>
    </section>

    <section id="letterScreen">
      <div class="paper">
        <h2>Quiero que sepas algo…</h2>
        <div id="letterContent"></div>
        <p class="signature">Con cariño ❤️</p>
      </div>
    </section>
  </div>

  <script>
    const startScreen = document.getElementById("startScreen");
    const gameScreen = document.getElementById("gameScreen");
    const letterScreen = document.getElementById("letterScreen");

    const startBtn = document.getElementById("startBtn");
    const scoreLabel = document.getElementById("scoreLabel");
    const goalLabel = document.getElementById("goalLabel");
    const overlay = document.getElementById("overlayMessage");
    const overlayTitle = document.getElementById("overlayTitle");
    const overlayText = document.getElementById("overlayText");
    const overlayBtn = document.getElementById("overlayBtn");
    const letterContent = document.getElementById("letterContent");

    const canvas = document.getElementById("gameCanvas");
    const ctx = canvas.getContext("2d");

    let w = 0;
    let h = 0;
    let groundY = 0;

    let animationId = null;
    let running = false;
    let gameOver = false;
    let won = false;

    const GOAL = 20;
    let score = 0;
    let gameSpeed = 5.5;

    const runner = {
      x: 70,
      y: 0,
      width: 42,
      height: 58,
      vy: 0,
      gravity: 0.85,
      jumpPower: -13.5,
      grounded: true
    };

    let obstacles = [];
    let obstacleTimer = 0;
    let nextObstacleIn = 90;

    const fullLetter = [
      "No sabía muy bien cómo decirte esto de una forma distinta, así que preferí que llegaras aquí poco a poco.",
      "Quiero que sepas que valoro muchísimo tu amistad. De verdad me gusta compartir contigo, hablar contigo, verte y sentir tu presencia cerca.",
      "Tienes algo que me transmite paz, ternura y una energía muy bonita. Eres de esas personas que se vuelven especiales sin hacer esfuerzo.",
      "Y siendo completamente sincero contigo… también me gustas. Me atraes. Hay algo en ti que me mueve por dentro y que no quería seguir guardándome.",
      "No te digo esto para presionarte ni para cambiar lo bonito que existe entre nosotros. Te lo digo porque quería ser real contigo y porque eres importante para mí.",
      "Pase lo que pase, quería que supieras que ocupas un lugar muy especial en mi vida, y que esto nació desde lo más bonito que siento por ti."
    ];

    function resizeCanvas() {
      w = window.innerWidth;
      h = window.innerHeight;
      canvas.width = w;
      canvas.height = h - 70;
      groundY = canvas.height - 90;
      runner.y = groundY - runner.height;
    }

    window.addEventListener("resize", resizeCanvas);

    function startGame() {
      startScreen.classList.remove("active");
      letterScreen.classList.remove("active");
      gameScreen.classList.add("active");
      restartGame();
    }

    function resetGameState() {
      score = 0;
      gameSpeed = 5.5;
      obstacles = [];
      obstacleTimer = 0;
      nextObstacleIn = 80 + Math.random() * 40;

      runner.vy = 0;
      runner.grounded = true;

      scoreLabel.textContent = "Puntos: 0";
      goalLabel.textContent = "Meta: " + GOAL;

      gameOver = false;
      won = false;
    }

    function restartGame() {
      cancelAnimationFrame(animationId);
      running = false;
      overlay.style.display = "none";
      resizeCanvas();
      resetGameState();
      running = true;
      animationId = requestAnimationFrame(loop);
    }

    function jump() {
      if (!running || gameOver || won) return;
      if (overlay.style.display === "flex") return;

      if (runner.grounded) {
        runner.vy = runner.jumpPower;
        runner.grounded = false;
      }
    }

    function spawnObstacle() {
      const height = 36 + Math.random() * 38;
      const width = 20 + Math.random() * 18;

      obstacles.push({
        x: canvas.width + 20,
        y: groundY - height + 6,
        width,
        height
      });

      obstacleTimer = 0;
      nextObstacleIn = Math.max(55, 90 - score * 1.3) + Math.random() * 35;
    }

    function update() {
      obstacleTimer++;
      if (obstacleTimer >= nextObstacleIn) {
        spawnObstacle();
      }

      runner.vy += runner.gravity;
      runner.y += runner.vy;

      const floor = groundY - runner.height;
      if (runner.y >= floor) {
        runner.y = floor;
        runner.vy = 0;
        runner.grounded = true;
      }

      for (let i = obstacles.length - 1; i >= 0; i--) {
        const obs = obstacles[i];
        obs.x -= gameSpeed;

        if (obs.x + obs.width < 0) {
          obstacles.splice(i, 1);
          score++;
          scoreLabel.textContent = "Puntos: " + score;

          if (score % 5 === 0) {
            gameSpeed += 0.4;
          }

          if (score >= GOAL) {
            winGame();
            return;
          }
        } else if (isColliding(runner, obs)) {
          loseGame();
          return;
        }
      }
    }

    function isColliding(a, b) {
      const ax = a.x + 6;
      const ay = a.y + 6;
      const aw = a.width - 12;
      const ah = a.height - 8;

      return (
        ax < b.x + b.width &&
        ax + aw > b.x &&
        ay < b.y + b.height &&
        ay + ah > b.y
      );
    }

    function drawBackground() {
      const sky = ctx.createLinearGradient(0, 0, 0, canvas.height);
      sky.addColorStop(0, "#1c1714");
      sky.addColorStop(1, "#3a2d24");
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      ctx.fillStyle = "rgba(255, 219, 171, 0.08)";
      ctx.beginPath();
      ctx.arc(canvas.width - 80, 80, 42, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#5a4538";
      ctx.fillRect(0, groundY + 4, canvas.width, canvas.height - groundY);

      ctx.fillStyle = "#7a5d49";
      for (let i = 0; i < canvas.width; i += 24) {
        ctx.fillRect(i, groundY + 8, 12, 3);
      }
    }

    function drawRunner() {
      const x = runner.x;
      const y = runner.y;

      ctx.save();

      ctx.fillStyle = "#f2d8b4";
      ctx.beginPath();
      ctx.arc(x + 22, y + 12, 9, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#b4835f";
      ctx.fillRect(x + 14, y + 22, 16, 20);

      ctx.strokeStyle = "#e8c39e";
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(x + 18, y + 43);
      ctx.lineTo(x + 14, y + 58);
      ctx.moveTo(x + 26, y + 43);
      ctx.lineTo(x + 30, y + 58);
      ctx.moveTo(x + 14, y + 28);
      ctx.lineTo(x + 7, y + 40);
      ctx.moveTo(x + 30, y + 28);
      ctx.lineTo(x + 38, y + 40);
      ctx.stroke();

      ctx.fillStyle = "#6b3f2a";
      ctx.beginPath();
      ctx.arc(x + 22, y + 9, 9, Math.PI, 0);
      ctx.fill();

      ctx.restore();
    }

    function drawObstacles() {
      obstacles.forEach(obs => {
        ctx.fillStyle = "#8a4f3b";
        ctx.fillRect(obs.x, obs.y, obs.width, obs.height);

        ctx.fillStyle = "#b77861";
        ctx.fillRect(obs.x + 3, obs.y + 3, obs.width - 6, 6);
      });
    }

    function draw() {
      drawBackground();
      drawRunner();
      drawObstacles();
    }

    function loop() {
      if (!running) return;
      update();
      draw();
      animationId = requestAnimationFrame(loop);
    }

    function loseGame() {
      running = false;
      gameOver = true;
      cancelAnimationFrame(animationId);

      overlayTitle.textContent = "Casi...";
      overlayText.textContent = "Toca el botón y vuelve a intentarlo.";
      overlay.style.display = "flex";
    }

    function winGame() {
      running = false;
      won = true;
      cancelAnimationFrame(animationId);
      setTimeout(showLetter, 700);
    }

    function showLetter() {
      gameScreen.classList.remove("active");
      letterScreen.classList.add("active");
      renderLetterAnimated(fullLetter);
    }

    function renderLetterAnimated(lines) {
      letterContent.innerHTML = "";
      lines.forEach((line, index) => {
        const p = document.createElement("p");
        p.className = "typeLine";
        p.style.animationDelay = `${index * 1.1}s`;
        p.textContent = line;
        letterContent.appendChild(p);
      });
    }

    startBtn.addEventListener("click", startGame);

    overlayBtn.addEventListener("click", function(e) {
      e.preventDefault();
      e.stopPropagation();
      restartGame();
    });

    overlayBtn.addEventListener("touchstart", function(e) {
      e.preventDefault();
      e.stopPropagation();
      restartGame();
    }, { passive: false });

    canvas.addEventListener("touchstart", function(e) {
      if (!gameScreen.classList.contains("active")) return;
      if (overlay.style.display === "flex") return;
      e.preventDefault();
      jump();
    }, { passive: false });

    canvas.addEventListener("mousedown", function() {
      if (!gameScreen.classList.contains("active")) return;
      if (overlay.style.display === "flex") return;
      jump();
    });

    resizeCanvas();
  </script>
</body>
</html>