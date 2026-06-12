/**
 * Abingdon People — CV builder
 *
 * Reads:  <cv_json_path>     — ParsedCV as JSON (from Claude parsing)
 *         <header_img_path>  — branded header PNG (empty string if none)
 * Writes: <output_path>      — formatted .docx
 *
 * Called by the Dresscode worker:
 *   node cv_builder.js <cv_json_path> <header_img_path> <output_path>
 *
 * Style rules (confirmed 2026-06-03 from reference DOCX — Bradley Viner):
 *   - Calibri 11pt throughout, all black, no colours or borders
 *   - NoSpacing (0pt before/after) — blank paragraphs create visual gaps
 *   - Branded header image: 794x200px PNG, flush to top, bleeds to left edge
 *   - Document title: "Firstname Lastname - CV" (en dash U+2013), bold, centred
 *   - Post-nominals (candidate.credentials): second line, bold, centred, if present
 *   - Section headings: bold, left-aligned, not underlined, not ALL CAPS
 *   - Section labels: Summary | Education | Skills | Employment | [extra as named]
 *   - Education: Hyperion style -- one bullet per entry, "Qualification - Institution, Year"
 *   - Skills: one bullet per skill phrase, positioned after Education
 *   - Employment: Date -> Company -> Title (all bold, separate lines), then bullet responsibilities
 *   - Extra sections: original heading text preserved, global heading style, bullet items
 *   - No references footer; no contact details
 */

'use strict';

const {
  Document, Packer, Paragraph, TextRun, Header, ImageRun,
  AlignmentType, LevelFormat
} = require('docx');
const fs = require('fs');

// -- CLI args ------------------------------------------------------------------
const [cvPath, headerImgPath, outputPath] = process.argv.slice(2);

if (!cvPath || !outputPath) {
  console.error('Usage: node cv_builder.js <cv_json_path> <header_img_path_or_empty> <output_path>');
  process.exit(1);
}

// -- Load inputs ---------------------------------------------------------------
const cv          = JSON.parse(fs.readFileSync(cvPath, 'utf8'));
const headerImage = headerImgPath && fs.existsSync(headerImgPath)
  ? fs.readFileSync(headerImgPath)
  : null;

// -- Style constants -----------------------------------------------------------
const FONT = 'Calibri';
const SIZE = 22;                        // half-points; 22 = 11pt
const NS   = { before: 0, after: 0 };  // NoSpacing

// -- Helpers -------------------------------------------------------------------

/** Bullet list numbering config */
function makeBulletConfig(ref) {
  return {
    reference: ref,
    levels: [{
      level: 0,
      format: LevelFormat.BULLET,
      text: '•',
      alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 720, hanging: 360 } } }
    }]
  };
}

/** Bold paragraph (left-aligned by default) */
function B(text, alignment) {
  return new Paragraph({
    spacing: NS,
    alignment: alignment || AlignmentType.LEFT,
    children: [new TextRun({ text: text || '', bold: true, font: FONT, size: SIZE })]
  });
}

/** Regular-weight paragraph */
function R(text) {
  return new Paragraph({
    spacing: NS,
    children: [new TextRun({ text: text || '', font: FONT, size: SIZE })]
  });
}

/** Empty separator line */
function blank() {
  return new Paragraph({
    spacing: NS,
    children: [new TextRun({ text: '', font: FONT, size: SIZE })]
  });
}

/** Bullet point */
function bullet(text, ref) {
  return new Paragraph({
    numbering: { reference: ref, level: 0 },
    spacing: NS,
    children: [new TextRun({ text: (text || '').trim(), font: FONT, size: SIZE })]
  });
}

/** Section heading -- bold, left-aligned, no underline, no ALL CAPS */
function sectionHeading(text) {
  return new Paragraph({
    spacing: NS,
    alignment: AlignmentType.LEFT,
    children: [new TextRun({ text: text || '', bold: true, font: FONT, size: SIZE })]
  });
}

/** Strip leading bullet/dash characters Claude sometimes includes */
function cleanBullet(text) {
  return (text || '').replace(/^[-•*]\s*/, '').trim();
}

/** Never render an ALL-CAPS heading: if the title has no lowercase letters,
 *  convert it to Title Case; otherwise keep the candidate's original casing. */
function normalizeHeading(text) {
  const t = (text || '').trim();
  if (!t || /[a-z]/.test(t)) return t;
  return t.replace(/[A-Za-z']+/g, w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
}

// -- Document content ----------------------------------------------------------
const rawName     = cv.candidate?.full_name || 'Candidate';
// Normalise to title case so ALL-CAPS source CVs render correctly
// Use [A-Za-z']+ so hyphens are preserved and each part is title-cased independently
const name        = rawName.replace(/[A-Za-z']+/g, w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
const credentials = cv.candidate?.credentials || null;
const children    = [];

// Blank line separates header image from document title
if (headerImage) children.push(blank());

// Document title: "Firstname Lastname - CV" (U+2013 en dash), bold, centred
children.push(new Paragraph({
  spacing: NS,
  alignment: AlignmentType.CENTER,
  children: [new TextRun({
    text: name + ' – CV',
    bold: true,
    font: FONT,
    size: SIZE
  })]
}));

// Post-nominals: second line, bold, centred (only when present in ParsedCV)
if (credentials) {
  children.push(new Paragraph({
    spacing: NS,
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: credentials, bold: true, font: FONT, size: SIZE })]
  }));
}

// -- Summary -------------------------------------------------------------------
if (cv.summary) {
  children.push(blank());
  children.push(sectionHeading('Summary'));
  children.push(blank());
  children.push(R(cv.summary));
}

// -- Education -----------------------------------------------------------------
if (cv.education && cv.education.length > 0) {
  children.push(blank());
  children.push(sectionHeading('Education'));
  children.push(blank());

  for (const edu of cv.education) {
    // Format: "Qualification - [Grade - ]Institution[, Year]"
    const parts = [];
    if (edu.qualification) parts.push(edu.qualification);
    if (edu.grade)         parts.push(edu.grade);
    if (edu.institution)   parts.push(edu.institution);

    let line = parts.join(' – ');
    if (edu.year) line += ', ' + edu.year;

    children.push(bullet(line, 'bullets-edu'));
  }
}

// -- Skills --------------------------------------------------------------------
if (cv.skills && cv.skills.length > 0) {
  children.push(blank());
  children.push(sectionHeading('Skills'));
  children.push(blank());

  for (const skill of cv.skills) {
    children.push(bullet(skill, 'bullets-skills'));
  }
}

// -- Employment ----------------------------------------------------------------
if (cv.experience && cv.experience.length > 0) {
  children.push(blank());
  children.push(sectionHeading('Employment'));
  children.push(blank());

  cv.experience.forEach(function(job, idx) {
    var bulletRef = 'bullets-r' + (idx + 1);

    // Blank line between roles (not before the first)
    if (idx > 0) children.push(blank());

    // Date line: "YYYY - YYYY" or "YYYY - Present"
    var dateParts = [];
    if (job.start_date)      dateParts.push(job.start_date);
    if (job.end_date)        dateParts.push(job.end_date);
    else if (job.start_date) dateParts.push('Present');
    var dateLine = dateParts.join(' – ');

    // Three bold header lines: date, company, title
    if (dateLine)    children.push(B(dateLine));
    if (job.company) children.push(B(job.company));
    if (job.title)   children.push(B(job.title));

    // Bullet responsibilities
    (job.responsibilities || []).forEach(function(resp) {
      var text = cleanBullet(resp);
      if (text) children.push(bullet(text, bulletRef));
    });
  });
}

// -- Extra sections ------------------------------------------------------------
// Original heading text preserved; formatted with global heading style
// (bold, left-aligned, no underline, no ALL CAPS)
// Reference/referee sections are suppressed
const SUPPRESS_SECTIONS = ['reference', 'referee'];
if (cv.extra_sections && cv.extra_sections.length > 0) {
  cv.extra_sections.forEach(function(section, idx) {
    if (!section.title) return;
    if (SUPPRESS_SECTIONS.some(function(s) { return section.title.toLowerCase().includes(s); })) return;
    var bulletRef = 'bullets-extra-' + idx;
    children.push(blank());
    children.push(sectionHeading(normalizeHeading(section.title)));
    children.push(blank());
    (section.items || []).forEach(function(item) {
      var text = cleanBullet(item);
      if (text) children.push(bullet(text, bulletRef));
    });
  });
}

// -- Bullet configs (edu, skills, up to 20 roles, up to 10 extra) --------------
const bulletConfigs = [
  makeBulletConfig('bullets-edu'),
  makeBulletConfig('bullets-skills'),
  ...Array.from({ length: 20 }, function(_, i) { return makeBulletConfig('bullets-r' + (i + 1)); }),
  ...Array.from({ length: 10 }, function(_, i) { return makeBulletConfig('bullets-extra-' + i); })
];

// -- Header image (Abingdon People branded banner, 794x200px) ------------------
const sectionHeaders = headerImage
  ? {
      default: new Header({
        children: [
          new Paragraph({
            spacing: NS,
            indent: { left: -1440 },
            children: [
              new ImageRun({
                type: 'png',
                data: headerImage,
                transformation: { width: 794, height: 200 },
                altText: { title: 'Header', description: 'Abingdon People header', name: 'Header' }
              })
            ]
          })
        ]
      })
    }
  : {};

// -- Assemble document ---------------------------------------------------------
const doc = new Document({
  numbering: { config: bulletConfigs },
  styles: {
    default: { document: { run: { font: FONT, size: SIZE } } }
  },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838 },
        margin: {
          top: 1440, right: 1440, bottom: 1440, left: 1440,
          header: 0,
          footer: 708
        }
      }
    },
    headers: sectionHeaders,
    children: children
  }]
});

Packer.toBuffer(doc)
  .then(function(buf) {
    fs.writeFileSync(outputPath, buf);
    console.log('Done:', outputPath);
  })
  .catch(function(err) {
    console.error('Builder error:', err);
    process.exit(1);
  });
