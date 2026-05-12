"""Converters for Confluence Storage Format (XHTML) to/from Markdown."""

import re
from bs4 import BeautifulSoup
from markdownify import markdownify


def storage_to_markdown(storage_content: str) -> str:
    """
    Convert Confluence Storage Format (XHTML) to Markdown.

    This is a lossy conversion - Confluence macros and some formatting
    will be simplified or lost. Use only for read-only display purposes.

    Args:
        storage_content: Confluence Storage Format content

    Returns:
        Markdown representation of the content
    """
    if not storage_content:
        return ""

    # Parse the XHTML content
    soup = BeautifulSoup(storage_content, 'html.parser')

    # Handle Confluence macros - convert some common ones to readable text
    _convert_confluence_macros(soup)

    # Convert to markdown using markdownify
    markdown = markdownify(
        str(soup),
        heading_style="ATX",  # Use # style headings
        bullets="-",  # Use - for bullet points
        strip=['script', 'style']
    )

    # Clean up extra whitespace
    markdown = re.sub(r'\n\s*\n\s*\n', '\n\n', markdown)
    markdown = markdown.strip()

    return markdown


def _convert_confluence_macros(soup: BeautifulSoup) -> None:
    """
    Convert common Confluence macros to readable text.

    This handles some basic macros by replacing them with text representations.
    More complex macros are left as-is or removed.
    """
    # Convert info/warning/note macros
    for macro in soup.find_all('ac:structured-macro'):
        macro_name = macro.get('ac:name', '')

        if macro_name in ['info', 'warning', 'note', 'tip']:
            # Extract the body content
            body = macro.find('ac:rich-text-body')
            if body:
                # Create a new div with class indicating the macro type
                new_div = soup.new_tag('div', **{'class': f'confluence-{macro_name}'})
                new_div.string = f"[{macro_name.upper()}] "
                new_div.extend(body.contents)
                macro.replace_with(new_div)
            else:
                macro.replace_with(f"[{macro_name.upper()}]")

        elif macro_name == 'code':
            # Convert code macros to code blocks
            body = macro.find('ac:plain-text-body')
            if body:
                new_pre = soup.new_tag('pre')
                new_code = soup.new_tag('code')
                new_code.string = body.get_text()
                new_pre.append(new_code)
                macro.replace_with(new_pre)

        elif macro_name == 'toc':
            # Replace table of contents with placeholder
            macro.replace_with("[Table of Contents]")

        # For other macros, just replace with the macro name
        else:
            macro.replace_with(f"[{macro_name} macro]")

    # Remove or convert other Confluence-specific elements
    for element in soup.find_all(['ac:link', 'ac:image']):
        if element.name == 'ac:link':
            # Try to extract link text and reference
            link_text = element.get_text()
            if link_text:
                element.replace_with(link_text)
            else:
                element.replace_with("[Link]")
        elif element.name == 'ac:image':
            # Replace images with placeholder
            element.replace_with("[Image]")


def markdown_to_storage_hint() -> str:
    """
    Returns a hint about markdown to storage conversion.

    Since we don't implement bidirectional conversion (it's complex and lossy),
    this provides guidance for users who want to create content.
    """
    return """
Note: This server accepts Confluence Storage Format (XHTML) for content creation/updates.
For simple content, you can use basic HTML tags:

- Headings: <h1>Title</h1>, <h2>Subtitle</h2>, etc.
- Paragraphs: <p>Text content</p>
- Lists: <ul><li>Item</li></ul> or <ol><li>Item</li></ol>
- Links: <a href="URL">Link text</a>
- Bold/Italic: <strong>Bold</strong>, <em>Italic</em>
- Code: <code>inline code</code> or <pre>code block</pre>

For advanced Confluence features (macros, page links), use the Confluence editor
or refer to the Storage Format documentation.
"""