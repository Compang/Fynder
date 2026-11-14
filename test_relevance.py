"""Deterministic relevance tests; no running Ollama required."""
import json
from pathlib import Path
import math
import tempfile
import unittest
from unittest.mock import patch
import app


class RelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.index = app.Index(Path(self.temp.name) / 'index.sqlite')
        rows = [
            ('towns.pdf', '1', 'American towns and their history', .8),
            ('towns.pdf', '2', 'A second account of American towns', .75),
            ('related.pdf', '1', 'Municipal history in the United States', .55),
            ('weak.pdf', '1', 'Regional geography', .38),
            ('unrelated.pdf', '1', 'Articles about engine repair', .1),
        ]
        with self.index.connect() as db:
            for path, page, text, similarity in rows:
                vector = json.dumps([similarity, math.sqrt(1-similarity**2)])
                db.execute('INSERT INTO chunks(path,location,text,vector,model) VALUES(?,?,?,?,?)',
                           (path, page, text, vector, 'test'))

    @patch('app.embed', return_value=[1, 0])
    def test_controls_are_nested_and_weak_matches_filtered(self, embed):
        results = {mode: self.index.find('articles about american towns', 'test', mode)
                   for mode in app.RELEVANCE}
        paths = {mode: {hit[1] for hit in hits} for mode, hits in results.items()}
        self.assertEqual(paths['Broad'], {'towns.pdf', 'related.pdf', 'weak.pdf'})
        self.assertEqual(paths['Balanced'], {'towns.pdf', 'related.pdf'})
        self.assertEqual(paths['Strict'], {'towns.pdf'})
        self.assertEqual(len(results['Broad']), 3)  # one result per document
        self.assertAlmostEqual(results['Strict'][0][4], 83)
        self.assertEqual(results['Strict'][0][2], '1')
        self.assertEqual(len(results['Strict'][0][7]), 2)
        self.assertEqual(results['Strict'][0][7][0][2], '1')

    def test_highlight_words_ignore_filler_and_match_plurals(self):
        text = 'Articles about American towns and townships.'
        spans = app.keyword_spans(text, 'articles about american town')
        self.assertEqual([text[a:b] for a, b in spans], ['American', 'towns'])

    def test_keyword_filler_and_plurals(self):
        hits = self.index.find('articles about american town', relevance='Strict')
        self.assertEqual([h[1] for h in hits], ['towns.pdf'])
        self.assertEqual(hits[0][4], 100)
        self.assertEqual(self.index.find('articles about'), [])

    @patch('app.embed', return_value=[1, 0])
    def test_wrong_model_does_not_bypass_filter(self, embed):
        logs = []
        self.assertEqual(self.index.find('American towns', 'other-model', status=logs.append), [])
        self.assertTrue(any('lack compatible embeddings' in line for line in logs))


if __name__ == '__main__':
    unittest.main()
