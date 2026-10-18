import React, {useEffect, useRef, useState} from 'react';
import {ChevronDown} from 'lucide-react';
import {PAGE, PAGE_HOME} from './keywordState';

export const MAIN_NAV_ITEMS = [
  {id: PAGE_HOME, label: '总览', path: 'home'},
  {id: PAGE.TAGGING, label: '商品打标', path: 'tagging'},
  {id: 'product-archive', label: '选品档案', path: 'product-archive'},
  {id: PAGE.KEYWORD, label: '关键词调研', path: 'keyword'},
  {id: 'amazon-reviews-voc', label: 'Amazon 评论 + VOC', path: 'amazon-reviews-voc'},
  {id: PAGE.AMAZON_SCRAPER, label: 'Amazon 图片采集', path: 'amazon-scraper'},
  {id: PAGE.XUNLONGJUE, label: '寻龙诀', path: 'xunlongjue'},
  {id: PAGE.SETTINGS, label: '总设置', path: 'settings'},
];

const REVIEW_PAGES = new Set(['amazon-reviews', 'amazon-reviews-voc', PAGE.AMAZON_VOC]);
const PROJECT_ITEM = {id: 'projects', label: '项目资料', path: 'projects'};

export function selectedNavigationId(page) {
  return REVIEW_PAGES.has(page) ? 'amazon-reviews-voc' : page;
}

export function navigationPageLabel(page) {
  if (page === 'projects') return PROJECT_ITEM.label;
  if (page === PAGE.USAGE) return '用量统计';
  return MAIN_NAV_ITEMS.find(item => item.id === selectedNavigationId(page))?.label || '总览';
}

export function WorkbenchNavigation({page, routeKey, navigate}) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef(null);
  const triggerRef = useRef(null);
  const selectedId = selectedNavigationId(page);

  useEffect(() => setOpen(false), [routeKey]);
  useEffect(() => {
    const compact = window.matchMedia('(max-width: 900px)');
    const closeWhenExpanded = event => {
      if (!event.matches) setOpen(false);
    };
    compact.addEventListener('change', closeWhenExpanded);
    return () => compact.removeEventListener('change', closeWhenExpanded);
  }, []);
  useEffect(() => {
    if (!open) return;
    const onPointerDown = event => {
      if (!menuRef.current?.contains(event.target)) setOpen(false);
    };
    const onKeyDown = event => {
      if (event.key !== 'Escape') return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const goTo = item => {
    if (open) triggerRef.current?.focus();
    setOpen(false);
    navigate(item.path);
  };
  const navButton = item => (
    <button
      type="button"
      key={item.id}
      className={selectedId === item.id ? 'active' : ''}
      aria-current={selectedId === item.id ? 'page' : undefined}
      onClick={() => goTo(item)}
    >{item.label}</button>
  );

  return <>
    <nav className="global-nav" aria-label="主导航">{MAIN_NAV_ITEMS.map(navButton)}</nav>
    <div className="compact-navigation" ref={menuRef}>
      <button
        type="button"
        className="compact-navigation-trigger"
        ref={triggerRef}
        aria-expanded={open}
        aria-controls="compact-navigation-menu"
        onClick={() => setOpen(value => !value)}
      ><span>当前：{navigationPageLabel(page)}</span><span>切换页面 <ChevronDown size={15}/></span></button>
      <nav id="compact-navigation-menu" className="compact-navigation-menu" aria-label="主导航" hidden={!open}>
        {MAIN_NAV_ITEMS.map(navButton)}
      </nav>
    </div>
  </>;
}
