"""Tests for chunking utilities."""

import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.chunking import clamp_chunk_size, iter_chunks
from framing.frame import MAX_PAYLOAD_SIZE


class TestChunkingUtilities(unittest.TestCase):
    """Test chunking utility functions."""
    
    def test_clamp_chunk_size_zero(self):
        """Test clamping with zero chunk size."""
        self.assertEqual(clamp_chunk_size(0), MAX_PAYLOAD_SIZE)
    
    def test_clamp_chunk_size_negative(self):
        """Test clamping with negative chunk size."""
        self.assertEqual(clamp_chunk_size(-10), MAX_PAYLOAD_SIZE)
    
    def test_clamp_chunk_size_within_max(self):
        """Test clamping with chunk size within max."""
        self.assertEqual(clamp_chunk_size(100), 100)
    
    def test_clamp_chunk_size_exceeds_max(self):
        """Test clamping with chunk size exceeding max."""
        self.assertEqual(clamp_chunk_size(MAX_PAYLOAD_SIZE + 100), MAX_PAYLOAD_SIZE)
    
    def test_clamp_chunk_size_exact_max(self):
        """Test clamping with chunk size exactly at max."""
        self.assertEqual(clamp_chunk_size(MAX_PAYLOAD_SIZE), MAX_PAYLOAD_SIZE)
    
    def test_iter_chunks_empty_data(self):
        """Test chunking empty data."""
        chunks = list(iter_chunks(b"", 10))
        self.assertEqual(chunks, [])
    
    def test_iter_chunks_exact_multiple(self):
        """Test chunking data that is exact multiple of chunk size."""
        data = b"0123456789" * 3  # 30 bytes
        chunks = list(iter_chunks(data, 10))
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0], b"0123456789")
        self.assertEqual(chunks[1], b"0123456789")
        self.assertEqual(chunks[2], b"0123456789")
    
    def test_iter_chunks_non_exact_multiple(self):
        """Test chunking data that is not exact multiple of chunk size."""
        data = b"0123456789ABC"  # 13 bytes
        chunks = list(iter_chunks(data, 10))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], b"0123456789")
        self.assertEqual(chunks[1], b"ABC")
    
    def test_iter_chunks_single_chunk(self):
        """Test chunking data smaller than chunk size."""
        data = b"small"
        chunks = list(iter_chunks(data, 100))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], b"small")
    
    def test_iter_chunks_zero_chunk_size(self):
        """Test chunking with zero chunk size defaults to MAX_PAYLOAD_SIZE."""
        data = b"test data"
        chunks = list(iter_chunks(data, 0))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], b"test data")
    
    def test_iter_chunks_negative_chunk_size(self):
        """Test chunking with negative chunk size defaults to MAX_PAYLOAD_SIZE."""
        data = b"test data"
        chunks = list(iter_chunks(data, -5))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], b"test data")
    
    def test_iter_chunks_preserves_data(self):
        """Test that chunking preserves all data when reassembled."""
        data = b"Lorem ipsum dolor sit amet, consectetur adipiscing elit." * 10
        chunks = list(iter_chunks(data, 50))
        reassembled = b"".join(chunks)
        self.assertEqual(reassembled, data)
    
    def test_iter_chunks_respects_max_payload_size(self):
        """Test that chunks never exceed MAX_PAYLOAD_SIZE."""
        large_chunk_size = MAX_PAYLOAD_SIZE + 1000
        data = b"x" * (MAX_PAYLOAD_SIZE * 2)
        chunks = list(iter_chunks(data, large_chunk_size))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_PAYLOAD_SIZE)


if __name__ == '__main__':
    unittest.main()
