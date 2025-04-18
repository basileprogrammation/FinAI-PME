document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('menu-btn');
    const mobileMenu = document.getElementById('mobile-menu');
  
    btn?.addEventListener('click', () => {
      mobileMenu.classList.toggle('hidden');
    });
  });
  