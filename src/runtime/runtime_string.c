// Copyright 2009 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

#include "runtime.h"

static	int32	empty		= 0;
string	emptystring	= (string)&empty;

int32
findnull(int8 *s)
{
	int32 l;

	for(l=0; s[l]!=0; l++)
		;
	return l;
}

void
sys·catstring(string s1, string s2, string s3)
{
	uint32 l;

	if(s1 == nil || s1->len == 0) {
		s3 = s2;
		goto out;
	}
	if(s2 == nil || s2->len == 0) {
		s3 = s1;
		goto out;
	}

	l = s1->len + s2->len;

	s3 = mal(sizeof(s3->len)+l);
	s3->len = l;
	mcpy(s3->str, s1->str, s1->len);
	mcpy(s3->str+s1->len, s2->str, s2->len);

out:
	FLUSH(&s3);
}

static void
prbounds(int8* s, int32 a, int32 b, int32 c)
{
	int32 i;

	prints(s);
	prints(" ");
	sys·printint(a);
	prints("<");
	sys·printint(b);
	prints(">");
	sys·printint(c);
	prints("\n");
	throw("bounds");
}

uint32
cmpstring(string s1, string s2)
{
	uint32 i, l;
	byte c1, c2;

	if(s1 == nil)
		s1 = emptystring;
	if(s2 == nil)
		s2 = emptystring;

	l = s1->len;
	if(s2->len < l)
		l = s2->len;
	for(i=0; i<l; i++) {
		c1 = s1->str[i];
		c2 = s2->str[i];
		if(c1 < c2)
			return -1;
		if(c1 > c2)
			return +1;
	}
	if(s1->len < s2->len)
		return -1;
	if(s1->len > s2->len)
		return +1;
	return 0;
}

void
sys·cmpstring(string s1, string s2, int32 v)
{
	v = cmpstring(s1, s2);
	FLUSH(&v);
}

int32
strcmp(byte *s1, byte *s2)
{
	uint32 i;
	byte c1, c2;

	for(i=0;; i++) {
		c1 = s1[i];
		c2 = s2[i];
		if(c1 < c2)
			return -1;
		if(c1 > c2)
			return +1;
		if(c1 == 0)
			return 0;
	}
}

void
sys·slicestring(string si, int32 lindex, int32 hindex, string so)
{
	string s, str;
	int32 l;

	if(si == nil)
		si = emptystring;

	if(lindex < 0 || lindex > si->len ||
	   hindex < lindex || hindex > si->len) {
		sys·printpc(&si);
		prints(" ");
		prbounds("slice", lindex, si->len, hindex);
	}

	l = hindex-lindex;
	so = mal(sizeof(so->len)+l);
	so->len = l;
	mcpy(so->str, si->str+lindex, l);
	FLUSH(&so);
}

void
sys·indexstring(string s, int32 i, byte b)
{
	if(s == nil)
		s = emptystring;

	if(i < 0 || i >= s->len) {
		sys·printpc(&s);
		prints(" ");
		prbounds("index", 0, i, s->len);
	}

	b = s->str[i];
	FLUSH(&b);
}

/*
 * this is the plan9 runetochar
 * extended for 36 bits in 7 bytes
 * note that it truncates to 32 bits
 * through the argument passing.
 */
static int32
runetochar(byte *str, uint32 c)
{
	int32 i, n;
	uint32 mask, mark;

	/*
	 * one character in 7 bits
	 */
	if(c <= 0x07FUL) {
		str[0] = c;
		return 1;
	}

	/*
	 * every new character picks up 5 bits
	 * one less in the first byte and
	 * six more in an extension byte
	 */
	mask = 0x7ffUL;
	mark = 0xC0UL;
	for(n=1;; n++) {
		if(c <= mask)
			break;
		mask = (mask<<5) | 0x1fUL;
		mark = (mark>>1) | 0x80UL;
	}

	/*
	 * lay down the bytes backwards
	 * n is the number of extension bytes
	 * mask is the max codepoint
	 * mark is the zeroth byte indicator
	 */
	for(i=n; i>0; i--) {
		str[i] = 0x80UL | (c&0x3fUL);
		c >>= 6;
	}

	str[0] = mark|c;
	return n+1;
}

void
sys·intstring(int64 v, string s)
{
	int32 l;

	s = mal(sizeof(s->len)+8);
	s->len = runetochar(s->str, v);
	FLUSH(&s);
}

void
sys·byteastring(byte *a, int32 l, string s)
{
	s = mal(sizeof(s->len)+l);
	s->len = l;
	mcpy(s->str, a, l);
	FLUSH(&s);
}
