import { marked } from 'marked';
import hljs from 'highlight.js';
import { markedHighlight } from 'marked-highlight';
import 'highlight.js/styles/github-dark.css';

marked.use(markedHighlight({
    langPrefix: 'hljs language-',
    highlight(code: string, lang: string) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    }
}));

marked.setOptions({
    breaks: true,
    gfm: true
});

export function renderMarkdown(content: string): string {
    try {
        return marked.parse(content) as string;
    } catch (e) {
        console.error('Markdown parse error:', e);
        return content;
    }
}

export { hljs };
