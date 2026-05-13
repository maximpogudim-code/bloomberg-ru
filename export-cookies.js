// Вставь этот скрипт в консоль Chrome DevTools на странице bloomberg.com
// Откроется диалог скачивания файла cookies.json
// Затем положи cookies.json рядом с proxy.py

(function() {
  const cookies = document.cookie.split('; ').map(c => {
    const [name, ...rest] = c.split('=');
    return {
      name: name.trim(),
      value: rest.join('='),
      domain: '.bloomberg.com',
      path: '/',
      secure: true,
      httpOnly: false,
      sameSite: 'None'
    };
  });

  const json = JSON.stringify(cookies, null, 2);
  const blob = new Blob([json], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bloomberg-cookies.json';
  a.click();

  console.log('Экспортировано', cookies.length, 'кук. Файл: bloomberg-cookies.json');
  console.log('Положи его в папку bloomberg-proxy/ рядом с proxy.py');
})();
