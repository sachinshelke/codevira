document.addEventListener('DOMContentLoaded', () => {

    const followGlow = document.querySelector('.follow-glow');
    window.addEventListener('mousemove', (e) => {
        if (followGlow) {
            followGlow.style.left = `${e.clientX}px`;
            followGlow.style.top = `${e.clientY}px`;
        }
    });

    const reveals = document.querySelectorAll('.reveal');
    const revealObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('active');
            }
        });
    }, { threshold: 0.1 });

    reveals.forEach(el => revealObserver.observe(el));
});
