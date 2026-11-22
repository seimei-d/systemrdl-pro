import { Fragment, type ReactNode } from 'react';

type TextFormat = 'bold' | 'italic' | 'underline' | 'strikethrough';

/**
 * Render the small, safety-oriented subset of rich text used in SystemRDL
 * name/desc properties. Backticks are accepted as inline code; BBCode [code],
 * [b], [i], [u], and [s] tags are also supported.
 */
export function RichText({ text, className }: { text: string; className?: string }) {
  return <span className={className}>{parseRichText(text)}</span>;
}

function parseRichText(text: string): ReactNode[] {
  const output: ReactNode[] = [];
  let plain = '';
  let i = 0;

  const flushPlain = () => {
    if (plain) {
      output.push(<Fragment key={`text-${output.length}`}>{plain}</Fragment>);
      plain = '';
    }
  };

  while (i < text.length) {
    if (text[i] === '`') {
      flushPlain();
      const end = text.indexOf('`', i + 1);
      if (end >= 0) {
        output.push(
          <code className="rdl-rich-code" key={`code-${output.length}`}>
            {text.slice(i + 1, end)}
          </code>,
        );
        i = end + 1;
        continue;
      }
    }

    if (text.startsWith('[code]', i) || /^\[code(?:=[^\]]*)?\]/i.test(text.slice(i))) {
      flushPlain();
      const opening = text.slice(i).match(/^\[code(?:=[^\]]*)?\]/i);
      if (!opening) {
        plain += text[i++];
        continue;
      }
      const close = text.toLowerCase().indexOf('[/code]', i + opening[0].length);
      if (close < 0) {
        plain += text[i++];
        continue;
      }
      const code = text.slice(i + opening[0].length, close);
      output.push(
        <code className="rdl-rich-code rdl-rich-code-block" key={`code-${output.length}`}>
          {code}
        </code>,
      );
      i = close + '[/code]'.length;
      continue;
    }

    const tag = text.slice(i).match(/^\[(b|i|u|s)\]([\s\S]*?)\[\/\1\]/i);
    if (tag) {
      flushPlain();
      const format: TextFormat = ({
        b: 'bold',
        i: 'italic',
        u: 'underline',
        s: 'strikethrough',
      } as const)[tag[1].toLowerCase() as 'b' | 'i' | 'u' | 's'];
      const className = `rdl-rich-${format}`;
      output.push(
        <span className={className} key={`format-${output.length}`}>
          {parseRichText(tag[2])}
        </span>,
      );
      i += tag[0].length;
      continue;
    }

    plain += text[i++];
  }

  flushPlain();
  return output;
}
