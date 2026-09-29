Mizu Support — восстановленная версия

Что исправлено:
- HTML перенесены в templates/
- panel.js перенесён в static/
- Flask настроен на static/panel.js
- добавлены /login, /logout, /setup
- добавлены сессии операторов и роли operator/admin
- добавлены API операторов, профиля, пароля, настроек бота
- добавлены /stats, /operators, /settings и их API
- добавлен /api/updates для уведомлений панели
- добавлена миграция таблиц operators, bot_settings и messages.operator_id
- сохранены Telegram-обработчики, чаты, медиа и стикерпаки
- пароль хранится как PBKDF2-SHA256, токен Telegram не изменяется

Запуск Windows:
1. Откройте CMD/PowerShell в этой папке.
2. Установите зависимости:
   py -3.13 -m pip install -r requirements.txt
3. Проверьте token.txt — туда должен быть настоящий токен бота.
4. Запустите:
   py -3.13 bot.py
5. Откройте http://127.0.0.1:5000
6. При первом запуске создайте администратора.

Важно:
- Не публикуйте token.txt и support.db.
- Для внешнего доступа замените MIZU_SECRET_KEY на случайное значение.
