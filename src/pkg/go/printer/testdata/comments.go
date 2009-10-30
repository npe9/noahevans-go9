// Copyright 2009 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

// This is a package for testing comment placement by go/printer.
//
package main

import "fmt"  // fmt

const c0 = 0;  // zero
const (
	c1 = iota;  // c1
	c2;  // c2
)


// The SZ struct; it is empty.
type SZ struct {}

// The S0 struct; no field is exported.
type S0 struct {
	int;
	x, y, z int;  // 3 unexported fields
}

// The S1 struct; some fields are not exported.
type S1 struct {
	S0;
	A, B, C float;  // 3 exported fields
	D, b, c int;  // 2 unexported fields
}

// The S2 struct; all fields are exported.
type S2 struct {
	S1;
	A, B, C float;  // 3 exported fields
}

// The IZ interface; it is empty.
type SZ interface {}

// The I0 interface; no method is exported.
type I0 interface {
	f(x int) int;  // unexported method
}

// The I1 interface; some methods are not exported.
type I1 interface {
	I0;
	F(x float) float;  // exported methods
	g(x int) int;  // unexported method
}

// The I2 interface; all methods are exported.
type I2 interface {
	I0;
	F(x float) float;  // exported method
	G(x float) float;  // exported method
}

// This comment group should be separated
// with a newline from the next comment
// group.

// This comment should NOT be associated with the next declaration.

var x int;  // x
var ()


// This comment SHOULD be associated with the next declaration.
func f0() {
	const pi = 3.14;  // pi
	var s1 struct {}  /* an empty struct */ /* foo */
	// a struct constructor
	// --------------------
	var s2 struct {} = struct {}{};
	x := pi;
}
//
// NO SPACE HERE
//
func f1() {
	f0();
	/* 1 */
	// 2
	/* 3 */
	/* 4 */
	f0();
}


func _() {
	// this comment should be properly indented
}


func abs(x int) int {
	if x < 0 {  // the tab printed before this comment's // must not affect the remaining lines
		return -x;  // this statement should be properly indented
	}
	return x;
}


func typeswitch(x interface{}) {
	switch v := x.(type) {
	case bool, int, float:
	case string:
	default:
	}

	switch x.(type) {
	}

	switch v0, ok := x.(int); v := x.(type) {
	}

	switch v0, ok := x.(int); x.(type) {
	case bool, int, float:
		// this comment should be indented
	case string:
	default:
		// this comment should be indented
	}
	// this comment should not be indented
}

func _() {
	/* freestanding comment
	   aligned		line
	   aligned line
	*/
}

func _() {
	/* freestanding comment
	   aligned		line
	   aligned line
	   */
}

func _() {
	/* freestanding comment
	   aligned		line
	   aligned line */
}

func _() {
	/*	freestanding comment
		aligned		line
		aligned line
	*/
}

func _() {
	/*	freestanding comment
		aligned		line
		aligned line
		*/
}

func _() {
	/*	freestanding comment
		aligned		line
		aligned line */
}


/*
 * line
 * of
 * stars
 */

/* another line
 * of
 * stars */

/*	and another line
 *	of
 *	stars */

/*
aligned in middle
here
        not here
*/

/*
blank line in middle:

with no leading spaces on blank line.
*/

func _() {
	/*
	 * line
	 * of
	 * stars
	 */

	/*
	aligned in middle
	here
		not here
	*/

	/*
	blank line in middle:

	with no leading spaces on blank line.
*/
}


// Line comments with tabs
func _() {
var	finput		*bufio.Reader;			// input file
var	stderr		*bufio.Writer;
var	ftable		*bufio.Writer;			// y.go file
var	foutput		*bufio.Writer;			// y.output file

var	oflag		string;				// -o [y.go]		- y.go file
var	vflag		string;				// -v [y.output]	- y.output file
var	lflag		bool;				// -l			- disable line directives
}


// This comment is the last entry in this file. It must be printed.
