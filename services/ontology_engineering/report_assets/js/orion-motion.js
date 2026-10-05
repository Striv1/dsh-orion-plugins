(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var revealElements = Array.prototype.slice.call(document.querySelectorAll('.reveal'));
  var tableRows = Array.prototype.slice.call(document.querySelectorAll('tbody tr'));

  document.querySelectorAll('.grid').forEach(function (grid) {
    Array.prototype.forEach.call(grid.children, function (child, index) {
      child.style.setProperty('--d', (index * 70) + 'ms');
    });
  });

  function animateCount(element) {
    var target = Number(element.getAttribute('data-count'));
    if (!Number.isFinite(target)) return;
    var start = null;
    var duration = 900;
    function step(timestamp) {
      if (start === null) start = timestamp;
      var progress = Math.min((timestamp - start) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 2.2);
      element.textContent = String(Math.round(target * eased));
      if (progress < 1) window.requestAnimationFrame(step);
    }
    element.textContent = '0';
    window.requestAnimationFrame(step);
  }

  function revealImmediately() {
    revealElements.forEach(function (element) { element.classList.add('in'); });
    tableRows.forEach(function (row) { row.classList.add('in'); });
    document.querySelectorAll('.stat-value[data-count]').forEach(function (element) {
      element.textContent = element.getAttribute('data-count');
    });
  }

  if (reduceMotion || !('IntersectionObserver' in window)) {
    revealImmediately();
    return;
  }

  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      entry.target.classList.add('in');
      observer.unobserve(entry.target);
    });
  }, { threshold: .12, rootMargin: '0px 0px -40px 0px' });

  revealElements.forEach(function (element) { observer.observe(element); });
  tableRows.forEach(function (row, index) {
    row.style.setProperty('--d', ((index % 12) * 45) + 'ms');
    observer.observe(row);
  });

  var countObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      animateCount(entry.target);
      countObserver.unobserve(entry.target);
    });
  }, { threshold: .5 });

  document.querySelectorAll('.stat-value[data-count]').forEach(function (element) {
    countObserver.observe(element);
  });

  var tocLinks = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  var sections = Array.prototype.slice.call(document.querySelectorAll('main section[id]'));
  if (tocLinks.length && sections.length) {
    var tocObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        tocLinks.forEach(function (link) {
          link.classList.toggle('active', link.getAttribute('href') === '#' + entry.target.id);
        });
      });
    }, { threshold: .3, rootMargin: '-15% 0px -60% 0px' });
    sections.forEach(function (section) { tocObserver.observe(section); });
  }
}());
